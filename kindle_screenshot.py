#!/usr/bin/env python3
"""Kindle自動キャプチャGUIアプリ."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np
import pyautogui
from PIL import Image
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QComboBox,
    QDoubleSpinBox,
    QSpinBox,
    QProgressBar,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
import Quartz


pyautogui.PAUSE = 0.2
pyautogui.FAILSAFE = True

BASE_DIR = Path(__file__).resolve().parent
SESSION_ROOT = BASE_DIR / "sessions"
LOG_DIR = BASE_DIR / "logs"
DEFAULT_OUTPUT = BASE_DIR

SESSION_ROOT.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "kindle_capture.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("kindle_capture")


class PageDirection(Enum):
    RIGHT_TO_LEFT = ("left", "右開き（←）")
    LEFT_TO_RIGHT = ("right", "左開き（→）")

    @property
    def key(self) -> str:
        return self.value[0]

    @property
    def label(self) -> str:
        return self.value[1]


@dataclass
class KindleWindow:
    window_id: int
    title: str
    bounds: Tuple[int, int, int, int]
    pid: int


@dataclass
class CaptureSettings:
    window: KindleWindow
    direction: PageDirection
    wait_time: float
    total_pages: int
    session_dir: Path
    output_dir: Path
    pdf_quality: int = 95
    keep_raw: bool = True

    def summary(self) -> dict:
        return {
            "window_title": self.window.title,
            "window_id": self.window.window_id,
            "direction": self.direction.name,
            "wait_time": self.wait_time,
            "total_pages": self.total_pages,
            "session_dir": str(self.session_dir),
            "output_dir": str(self.output_dir),
            "pdf_quality": self.pdf_quality,
        }


class KindleWindowManager:
    """Kindleウィンドウ列挙ユーティリティ."""

    @staticmethod
    def list_windows() -> List[KindleWindow]:
        options = (
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements
        )
        window_info = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        windows: List[KindleWindow] = []
        for info in window_info:
            owner = info.get("kCGWindowOwnerName", "")
            if "Kindle" not in owner:
                continue
            bounds = info.get("kCGWindowBounds", {})
            width = int(bounds.get("Width", 0))
            height = int(bounds.get("Height", 0))
            if width == 0 or height == 0:
                continue
            title = info.get("kCGWindowName") or "Kindle Window"
            window_id = int(info.get("kCGWindowNumber"))
            windows.append(
                KindleWindow(
                    window_id=window_id,
                    title=title,
                    bounds=(
                        int(bounds.get("X", 0)),
                        int(bounds.get("Y", 0)),
                        width,
                        height,
                    ),
                    pid=int(info.get("kCGWindowOwnerPID", 0)),
                )
            )
        return windows


def activate_kindle_window(window: KindleWindow) -> None:
    script = f'''
        tell application "System Events"
            set frontmost of (first process whose unix id is {window.pid}) to true
        end tell
    '''
    subprocess.run(["osascript", "-e", script], check=False)
    time.sleep(1.0)


def capture_window_image(window: KindleWindow) -> Image.Image:
    x, y, w, h = window.bounds
    rect = Quartz.CGRectMake(x, y, w, h)
    image_ref = Quartz.CGWindowListCreateImage(
        rect,
        Quartz.kCGWindowListOptionIncludingWindow,
        window.window_id,
        Quartz.kCGWindowImageDefault,
    )
    if image_ref is None:
        raise RuntimeError("ウィンドウ画像の取得に失敗しました")
    width = Quartz.CGImageGetWidth(image_ref)
    height = Quartz.CGImageGetHeight(image_ref)
    bytes_per_row = Quartz.CGImageGetBytesPerRow(image_ref)
    data_provider = Quartz.CGImageGetDataProvider(image_ref)
    data = Quartz.CGDataProviderCopyData(data_provider)
    buffer = bytes(data)
    img = Image.frombuffer(
        "RGBA",
        (width, height),
        buffer,
        "raw",
        "BGRA",
        bytes_per_row,
        1,
    )
    return img.convert("RGB")


def structural_similarity(path_a: Path, path_b: Path) -> float:
    img_a = cv2.imread(str(path_a), cv2.IMREAD_GRAYSCALE)
    img_b = cv2.imread(str(path_b), cv2.IMREAD_GRAYSCALE)
    if img_a is None or img_b is None:
        return 0.0
    width = min(img_a.shape[1], img_b.shape[1], 768)
    height = min(img_a.shape[0], img_b.shape[0], 1024)
    if width <= 0 or height <= 0:
        return 0.0
    img_a = cv2.resize(img_a, (width, height), interpolation=cv2.INTER_AREA)
    img_b = cv2.resize(img_b, (width, height), interpolation=cv2.INTER_AREA)
    img_a = img_a.astype(np.float32)
    img_b = img_b.astype(np.float32)
    kernel = (11, 11)
    sigma = 1.5
    mu1 = cv2.GaussianBlur(img_a, kernel, sigma)
    mu2 = cv2.GaussianBlur(img_b, kernel, sigma)
    mu1_sq = mu1 * mu1
    mu2_sq = mu2 * mu2
    mu1_mu2 = mu1 * mu2
    sigma1_sq = cv2.GaussianBlur(img_a * img_a, kernel, sigma) - mu1_sq
    sigma2_sq = cv2.GaussianBlur(img_b * img_b, kernel, sigma) - mu2_sq
    sigma12 = cv2.GaussianBlur(img_a * img_b, kernel, sigma) - mu1_mu2
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    numerator = (2 * mu1_mu2 + c1) * (2 * sigma12 + c2)
    denominator = (mu1_sq + mu2_sq + c1) * (sigma1_sq + sigma2_sq + c2)
    ssim_map = numerator / (denominator + 1e-9)
    return float(ssim_map.mean())


class ScreenshotSession:
    def __init__(
        self,
        settings: CaptureSettings,
        stop_check: Callable[[], bool],
        progress_cb: Callable[[int, str], None],
    ) -> None:
        self.settings = settings
        self.stop_check = stop_check
        self.progress_cb = progress_cb
        self.image_paths: List[Path] = []
        self.same_counter = 0
        self.start_time = time.time()
        self.settings.session_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> Tuple[bool, Optional[Path]]:
        activate_kindle_window(self.settings.window)
        time.sleep(0.5)
        completed = False
        while not self.stop_check():
            index = len(self.image_paths) + 1
            screenshot_path = self.settings.session_dir / f"page_{index:04d}.png"
            try:
                capture_window_image(self.settings.window).save(screenshot_path)
            except Exception as exc:  # noqa: BLE001
                logger.exception("スクリーンショット取得失敗: %s", exc)
                screenshot_path.unlink(missing_ok=True)
                return False, None

            self.image_paths.append(screenshot_path)
            self.progress_cb(len(self.image_paths), "撮影中")

            if len(self.image_paths) >= self.settings.total_pages:
                completed = True
                break

            if self.stop_check():
                break

            pyautogui.press(self.settings.direction.key)
            time.sleep(self.settings.wait_time)

        if not self.image_paths:
            return False, None
        pdf_path = self._create_pdf()
        self._write_report(pdf_path, completed)
        return completed, pdf_path

    def _create_pdf(self) -> Path:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        output_name = f"{self.settings.window.title or 'Kindle_Book'}_{datetime.now().strftime('%Y%m%d_%H%M%S')}".replace("/", "-")
        pdf_path = self.settings.output_dir / f"{output_name}.pdf"
        images: List[Image.Image] = []
        for path in self.image_paths:
            with Image.open(path) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                images.append(img.copy())
        if images:
            images[0].save(
                pdf_path,
                "PDF",
                save_all=True,
                append_images=images[1:],
                quality=self.settings.pdf_quality,
            )
        for img in images:
            img.close()
        return pdf_path

    def _write_report(self, pdf_path: Path, completed: bool) -> None:
        report = {
            "captured_pages": len(self.image_paths),
            "completed": completed,
            "pdf_path": str(pdf_path),
            "settings": self.settings.summary(),
            "duration_sec": round(time.time() - self.start_time, 2),
        }
        (self.settings.session_dir / "session.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class CaptureWorker(QThread):
    progress = Signal(int, str)
    finished = Signal(bool, str)
    errored = Signal(str)

    def __init__(self, settings: CaptureSettings) -> None:
        super().__init__()
        self.settings = settings
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def run(self) -> None:
        session = ScreenshotSession(
            self.settings,
            self.is_stopped,
            self.progress.emit,
        )
        try:
            completed, pdf_path = session.run()
            if pdf_path is None:
                self.finished.emit(False, "")
            else:
                self.finished.emit(completed, str(pdf_path))
        except Exception as exc:  # noqa: BLE001
            logger.exception("キャプチャ処理で例外: %s", exc)
            self.errored.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Kindle Capture Studio")
        self.worker: Optional[CaptureWorker] = None
        self.latest_session: Optional[Path] = None
        self.latest_pdf: Optional[Path] = None
        self.current_session_dir: Optional[Path] = None
        self._build_ui()
        self.refresh_windows()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        self.window_combo = QComboBox()
        refresh_btn = QPushButton("再取得")
        refresh_btn.clicked.connect(self.refresh_windows)
        window_box = QHBoxLayout()
        window_box.addWidget(QLabel("Kindleウィンドウ"))
        window_box.addWidget(self.window_combo, 1)
        window_box.addWidget(refresh_btn)

        config_group = QGroupBox("設定")
        form = QFormLayout()
        self.direction_combo = QComboBox()
        for direction in PageDirection:
            self.direction_combo.addItem(direction.label, direction)
        self.wait_spin = QDoubleSpinBox()
        self.wait_spin.setRange(0.2, 5.0)
        self.wait_spin.setSingleStep(0.1)
        self.wait_spin.setValue(1.2)
        self.page_count_spin = QSpinBox()
        self.page_count_spin.setRange(1, 10000)
        self.page_count_spin.setValue(100)
        form.addRow("ページ方向", self.direction_combo)
        form.addRow("待機秒", self.wait_spin)
        form.addRow("総ページ数", self.page_count_spin)
        config_group.setLayout(form)

        self.output_label = QLabel(str(DEFAULT_OUTPUT))
        output_btn = QPushButton("変更")
        output_btn.clicked.connect(self.select_output_dir)
        output_box = QHBoxLayout()
        output_box.addWidget(QLabel("出力フォルダ"))
        output_box.addWidget(self.output_label, 1)
        output_box.addWidget(output_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.status_label = QLabel("待機中")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)

        buttons_box = QHBoxLayout()
        self.start_btn = QPushButton("開始")
        self.start_btn.clicked.connect(self.start_capture)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_capture)
        self.stop_btn.setEnabled(False)
        self.pdf_btn = QPushButton("PDF再生成")
        self.pdf_btn.clicked.connect(self.rebuild_pdf)
        self.pdf_btn.setEnabled(False)
        buttons_box.addWidget(self.start_btn)
        buttons_box.addWidget(self.stop_btn)
        buttons_box.addWidget(self.pdf_btn)

        layout.addLayout(window_box)
        layout.addWidget(config_group)
        layout.addLayout(output_box)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons_box)
        layout.addWidget(QLabel("ログ"))
        layout.addWidget(self.log_view, 1)

        self.setCentralWidget(root)
        self.resize(780, 640)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] {message}")

    def select_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "出力フォルダを選択", str(DEFAULT_OUTPUT))
        if directory:
            self.output_label.setText(directory)

    def refresh_windows(self) -> None:
        self.window_combo.clear()
        windows = KindleWindowManager.list_windows()
        if not windows:
            self.window_combo.addItem("Kindleウィンドウが見つかりません", None)
            self.log("Kindleウィンドウを検出できませんでした")
        else:
            for window in windows:
                label = f"{window.title} (ID:{window.window_id})"
                self.window_combo.addItem(label, window)
            self.log(f"{len(windows)}件のウィンドウを検出しました")

    def start_capture(self) -> None:
        window: Optional[KindleWindow] = self.window_combo.currentData()
        if window is None:
            QMessageBox.warning(self, "エラー", "Kindleウィンドウが見つかりません。")
            return
        session_dir = SESSION_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
        direction = self.direction_combo.currentData()
        if direction is None:
            direction = PageDirection.RIGHT_TO_LEFT
        settings = CaptureSettings(
            window=window,
            direction=direction,
            wait_time=self.wait_spin.value(),
            total_pages=self.page_count_spin.value(),
            session_dir=session_dir,
            output_dir=Path(self.output_label.text()),
        )
        self.current_session_dir = session_dir
        self.worker = CaptureWorker(settings)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.capture_finished)
        self.worker.errored.connect(self.capture_error)
        self.worker.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("撮影中…")
        self.progress_bar.setRange(0, 0)
        self.log("キャプチャを開始しました")

    def stop_capture(self) -> None:
        if self.worker:
            self.worker.request_stop()
            self.log("停止要求を送信しました")
        self.stop_btn.setEnabled(False)

    def update_progress(self, count: int, message: str) -> None:
        self.status_label.setText(f"{message} / {count}ページ")
        self.log(f"{message} - {count}ページ")

    def capture_finished(self, completed: bool, pdf_path: str) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setRange(0, 1)
        self.worker = None
        if pdf_path:
            self.latest_pdf = Path(pdf_path)
            self.pdf_btn.setEnabled(True)
            self.latest_session = self.current_session_dir or Path(pdf_path).parent
        msg = "完了" if completed else "中断" if pdf_path else "失敗"
        self.status_label.setText(msg)
        if pdf_path:
            self.log(f"PDF生成: {pdf_path}")
            QMessageBox.information(self, "完了", f"PDFを生成しました:\n{pdf_path}")
        else:
            QMessageBox.warning(self, "警告", "PDFを生成できませんでした")
        self.current_session_dir = None

    def capture_error(self, detail: str) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setRange(0, 1)
        self.worker = None
        self.status_label.setText("エラー")
        self.log(f"エラー: {detail}")
        QMessageBox.critical(self, "エラー", detail)
        self.current_session_dir = None

    def rebuild_pdf(self) -> None:
        if not self.latest_session:
            QMessageBox.warning(self, "警告", "再生成できるセッションがありません")
            return
        images = sorted(self.latest_session.glob("page_*.png"))
        if not images:
            QMessageBox.warning(self, "警告", "画像ファイルがありません")
            return
        settings = {
            "window_title": self.latest_session.name,
            "output_dir": self.output_label.text(),
        }
        output_name = f"rebuild_{self.latest_session.name}.pdf"
        pdf_path = Path(settings["output_dir"]) / output_name
        pil_images: List[Image.Image] = []
        for image in images:
            with Image.open(image) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                pil_images.append(img.copy())
        pil_images[0].save(pdf_path, "PDF", save_all=True, append_images=pil_images[1:])
        for img in pil_images:
            img.close()
        self.latest_pdf = pdf_path
        QMessageBox.information(self, "完了", f"PDFを再生成しました:\n{pdf_path}")
        self.log(f"PDF再生成: {pdf_path}")


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
