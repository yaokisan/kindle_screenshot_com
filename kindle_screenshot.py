#!/usr/bin/env python3
"""
Kindle自動スクリーンショットスクリプト
MacBookでKindleアプリを自動操作し、縦書き書籍の全ページをスクリーンショット撮影してPDFにまとめます。
"""

import pyautogui
import time
import os
from pathlib import Path
from PIL import Image
import sys

# AppKitのインポート（オプショナル）
try:
    from AppKit import NSWorkspace
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False

# pyautoguiの安全設定
pyautogui.PAUSE = 0.5
pyautogui.FAILSAFE = True

# 設定
PAGE_WAIT_TIME = 1.5  # ページ送り後の待機時間（秒）- パフォーマンス最適化
SCREENSHOT_DIR = Path("screenshots_temp")  # 一時スクリーンショット保存ディレクトリ
PDF_QUALITY = 95  # PDF品質（1-100）
SAME_PAGE_THRESHOLD = 0.80  # 同じページと判定する閾値（0.0-1.0、低いほど厳しい）- 精度向上
CONSECUTIVE_SAME_PAGE_LIMIT = 5  # 連続して同じページが検出された場合の終了判定回数（誤検出防止）
MIN_PAGES_BEFORE_CHECK = 3  # このページ数までは比較をスキップ（誤検出防止）


def activate_kindle_app():
    """Kindleアプリをアクティブ化する"""
    try:
        # 複数のアプリケーション名を試す
        app_names = ["Kindle", "com.amazon.Kindle"]
        
        for app_name in app_names:
            try:
                # AppleScriptを使用してKindleアプリをアクティブ化
                script = f'''
                tell application "{app_name}"
                    activate
                end tell
                '''
                result = os.system(f"osascript -e '{script}' 2>/dev/null")
                if result == 0:
                    time.sleep(1)  # アプリがアクティブになるまで待機
                    return True
            except:
                continue
        
        # System Eventsでプロセス名から検索
        script = '''
        tell application "System Events"
            set kindleProcess to first process whose name contains "Kindle"
            set frontmost of kindleProcess to true
        end tell
        '''
        result = os.system(f"osascript -e '{script}' 2>/dev/null")
        if result == 0:
            time.sleep(1)
            return True
        
        # エラーを無視して続行（ユーザーが手動でアクティブ化できる）
        print("警告: Kindleアプリの自動アクティブ化に失敗しましたが、続行します。")
        print("Kindleアプリのウィンドウを手動でアクティブにしてください。")
        time.sleep(2)
        return True
    except Exception as e:
        print(f"警告: Kindleアプリのアクティブ化でエラーが発生しました: {e}")
        print("Kindleアプリのウィンドウを手動でアクティブにしてください。")
        time.sleep(2)
        return True


def get_kindle_window_title():
    """Kindleアプリのウィンドウタイトル（本のタイトル）を取得する"""
    try:
        # AppKitが利用可能な場合
        if HAS_APPKIT:
            try:
                workspace = NSWorkspace.sharedWorkspace()
                running_apps = workspace.runningApplications()
                
                for app in running_apps:
                    app_name = app.localizedName()
                    if app_name and "Kindle" in app_name:
                        try:
                            windows = app.windows()
                            if windows and len(windows) > 0:
                                window_title = windows[0].title()
                                if window_title:
                                    return window_title
                        except:
                            pass
            except:
                pass  # AppKitでの取得に失敗した場合はAppleScriptにフォールバック
        
        # AppleScriptで取得（複数のプロセス名を試す）
        scripts = [
            '''
            tell application "System Events"
                tell process "Kindle"
                    set windowTitle to name of window 1
                    return windowTitle
                end tell
            end tell
            ''',
            '''
            tell application "System Events"
                set kindleProcess to first process whose name contains "Kindle"
                set windowTitle to name of window 1 of kindleProcess
                return windowTitle
            end tell
            '''
        ]
        
        for script in scripts:
            try:
                result = os.popen(f"osascript -e '{script}' 2>/dev/null").read().strip()
                if result and result != "" and "error" not in result.lower():
                    return result
            except:
                continue
        
        return "Kindle_Book"
    except Exception as e:
        print(f"警告: ウィンドウタイトルの取得に失敗しました: {e}")
        return "Kindle_Book"


def sanitize_filename(filename):
    """ファイル名に使用できない文字を削除・置換する"""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename.strip()


def take_screenshot(filepath):
    """スクリーンショットを撮影して保存する"""
    try:
        screenshot = pyautogui.screenshot()
        screenshot.save(filepath)
        return True
    except Exception as e:
        print(f"エラー: スクリーンショットの撮影に失敗しました: {e}")
        return False


def images_are_similar(img1_path, img2_path, threshold=None):
    """2つの画像が似ているか判定する（ページが変わったか検出）- パフォーマンス最適化版"""
    if threshold is None:
        threshold = SAME_PAGE_THRESHOLD
    
    try:
        # ファイルサイズで高速チェック（大幅に異なる場合は即座にFalse）
        size1 = img1_path.stat().st_size
        size2 = img2_path.stat().st_size
        if abs(size1 - size2) / max(size1, size2) > 0.1:  # 10%以上の差があれば異なるページ
            return False
        
        img1 = Image.open(img1_path)
        img2 = Image.open(img2_path)
        
        # 画像サイズが異なる場合は異なるページと判定
        if img1.size != img2.size:
            return False
        
        # RGBモードに変換（必要に応じて）
        if img1.mode != 'RGB':
            img1 = img1.convert('RGB')
        if img2.mode != 'RGB':
            img2 = img2.convert('RGB')
        
        # パフォーマンス最適化: より小さなサイズで比較し、サンプリングを使用
        # 縦書き書籍の場合、中央部分を重点的に比較
        compare_size = 150  # バランスの取れたサイズ
        img1_resized = img1.resize((compare_size, compare_size), Image.Resampling.LANCZOS)
        img2_resized = img2.resize((compare_size, compare_size), Image.Resampling.LANCZOS)
        
        # ピクセル値の差分を効率的に計算（NumPyを使わずに最適化）
        pixels1 = img1_resized.getdata()
        pixels2 = img2_resized.getdata()
        
        diff = 0
        pixel_count = 0
        # サンプリング: 全ピクセルではなく、一部をサンプルして比較（パフォーマンス向上）
        step = 2  # 2ピクセルごとにサンプル
        for i, (p1, p2) in enumerate(zip(pixels1, pixels2)):
            if i % step == 0:  # サンプリング
                if isinstance(p1, tuple) and isinstance(p2, tuple):
                    diff += sum(abs(a - b) for a, b in zip(p1, p2))
                else:
                    diff += abs(p1 - p2)
                pixel_count += 1
        
        # 差分が閾値以下なら同じページと判定
        max_diff = pixel_count * 255 * 3  # RGBの場合
        if max_diff == 0:
            return True  # エラー回避
        similarity = 1 - (diff / max_diff)
        
        return similarity >= threshold
    except Exception as e:
        # エラー時は安全のためFalseを返す（ページが変わったと判定）
        return False


def remove_duplicate_images(image_paths):
    """重複したスクリーンショットを削除する"""
    if len(image_paths) <= 1:
        return image_paths
    
    unique_paths = [image_paths[0]]  # 最初の画像は常に含める
    
    for i in range(1, len(image_paths)):
        current_path = image_paths[i]
        previous_path = unique_paths[-1]
        
        # 前の画像と比較して、異なる場合のみ追加
        if not images_are_similar(previous_path, current_path):
            unique_paths.append(current_path)
        else:
            # 重複している場合は、現在のファイルを削除
            try:
                if current_path.exists():
                    current_path.unlink()
            except:
                pass
    
    return unique_paths


def convert_images_to_pdf(image_paths, output_path):
    """複数の画像を1つのPDFに結合する（重複削除済み）"""
    try:
        if not image_paths:
            print("エラー: 結合する画像がありません")
            return False
        
        images = []
        for img_path in image_paths:
            if not img_path.exists():
                continue  # ファイルが存在しない場合はスキップ
            try:
                img = Image.open(img_path)
                # RGBモードに変換（PDFに変換するため）
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                images.append(img)
            except Exception as e:
                print(f"警告: {img_path} の読み込みに失敗しました: {e}")
                continue
        
        if images:
            images[0].save(
                output_path,
                "PDF",
                resolution=100.0,
                save_all=True,
                append_images=images[1:] if len(images) > 1 else []
            )
            print(f"PDFファイルを作成しました: {output_path}")
            return True
        else:
            print("エラー: 結合する画像がありません")
            return False
    except Exception as e:
        print(f"エラー: PDF変換に失敗しました: {e}")
        return False


def main():
    """メイン処理"""
    print("Kindle自動スクリーンショットスクリプトを開始します...")
    
    # Kindleアプリをアクティブ化
    print("Kindleアプリをアクティブ化しています...")
    activate_kindle_app()  # エラーでも続行（ユーザーが手動でアクティブ化可能）
    
    time.sleep(2)  # アプリが完全にアクティブになるまで待機
    
    # 本のタイトルを取得
    print("本のタイトルを取得しています...")
    book_title = get_kindle_window_title()
    sanitized_title = sanitize_filename(book_title)
    print(f"本のタイトル: {book_title}")
    
    # 一時ディレクトリを作成
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    
    # スクリーンショット撮影ループ
    print("\nスクリーンショット撮影を開始します...")
    print("最後のページに到達するまで自動的にページを進めます。")
    print("中断する場合は、マウスを画面の左上隅に移動してください。\n")
    
    page_count = 0
    image_paths = []
    previous_screenshot_path = None
    consecutive_same_page_count = 0  # 連続して同じページが検出された回数
    start_time = time.time()  # 開始時刻を記録
    
    try:
        while True:
            page_count += 1
            
            # 進行状況を表示（ユーザー体験向上）
            elapsed_time = time.time() - start_time
            pages_per_sec = page_count / elapsed_time if elapsed_time > 0 else 0
            print(f"[{page_count:4d}ページ] 撮影中... (速度: {pages_per_sec:.1f}ページ/秒)", end=" ", flush=True)
            
            # スクリーンショットを撮影
            screenshot_path = SCREENSHOT_DIR / f"page_{page_count:04d}.png"
            
            if not take_screenshot(screenshot_path):
                print("失敗")
                break
            
            # 前回のスクリーンショットと比較（MIN_PAGES_BEFORE_CHECKページ目以降のみ）
            is_same_page = False
            should_advance_page = True  # ページ送りを行うかどうか
            
            # 最初の数ページは比較をスキップ（誤検出防止）
            if page_count >= MIN_PAGES_BEFORE_CHECK and previous_screenshot_path and previous_screenshot_path.exists():
                is_same_page = images_are_similar(previous_screenshot_path, screenshot_path)
                
                if is_same_page:
                    consecutive_same_page_count += 1
                    print(f"✓ (同じページ: {consecutive_same_page_count}/{CONSECUTIVE_SAME_PAGE_LIMIT})")
                    
                    # 連続して同じページが検出された場合のみ終了
                    if consecutive_same_page_count >= CONSECUTIVE_SAME_PAGE_LIMIT:
                        print(f"\n✓ 連続{CONSECUTIVE_SAME_PAGE_LIMIT}回同じページが検出されました。最後のページに到達したと判定します。")
                        # 現在のスクリーンショットを削除（まだimage_pathsに追加されていない）
                        if screenshot_path.exists():
                            try:
                                screenshot_path.unlink()
                            except:
                                pass
                        # 重複した最後の数ページをimage_pathsから削除（存在する場合）
                        # ただし、実際に保存されているファイルは削除しない（PDF変換で使用するため）
                        for i in range(min(consecutive_same_page_count, len(image_paths))):
                            if image_paths:
                                image_paths.pop()
                        page_count -= consecutive_same_page_count
                        break
                    else:
                        # 同じページが検出されたが、まだ終了しない
                        # 現在のスクリーンショットを削除（まだimage_pathsに追加されていない）
                        if screenshot_path.exists():
                            try:
                                screenshot_path.unlink()
                            except:
                                pass
                        page_count -= 1
                        # もう一度ページ送りを試みる（次のループで実行）
                        should_advance_page = True
                else:
                    consecutive_same_page_count = 0  # ページが変わったのでリセット
            
            # ページが変わった場合
            if not is_same_page:
                image_paths.append(screenshot_path)
                previous_screenshot_path = screenshot_path
                consecutive_same_page_count = 0
                print("✓")
            
            # 次のページへ進む（左矢印キー）
            if should_advance_page:
                pyautogui.press('left')
                time.sleep(PAGE_WAIT_TIME)
    
    except KeyboardInterrupt:
        print("\n\nユーザーによって中断されました。")
    except pyautogui.FailSafeException:
        print("\n\nフェイルセーフが発動しました（マウスが左上隅に移動されました）。")
    except Exception as e:
        print(f"\n\nエラーが発生しました: {e}")
    
    # PDFに結合
    if image_paths:
        total_pages_before = len(image_paths)
        print(f"\n{'='*60}")
        print(f"✓ {total_pages_before}ページのスクリーンショットを撮影しました")
        print(f"{'='*60}")
        
        # 重複したスクリーンショットを削除
        print(f"\n重複したスクリーンショットを削除しています...", end=" ", flush=True)
        image_paths = remove_duplicate_images(image_paths)
        duplicates_removed = total_pages_before - len(image_paths)
        if duplicates_removed > 0:
            print(f"✓ ({duplicates_removed}個の重複を削除)")
        else:
            print("✓ (重複なし)")
        
        print(f"\nPDFファイルに結合しています...")
        
        output_pdf = Path(f"{sanitized_title}_screenshots.pdf")
        pdf_start_time = time.time()
        total_pages = len(image_paths)
        
        if convert_images_to_pdf(image_paths, output_pdf):
            pdf_elapsed = time.time() - pdf_start_time
            file_size = output_pdf.stat().st_size / (1024 * 1024)  # MB
            
            # 一時ファイルを削除（パフォーマンス最適化: 並列処理）
            print("一時ファイルを削除しています...", end=" ", flush=True)
            for img_path in image_paths:
                try:
                    if img_path.exists():
                        img_path.unlink()
                except:
                    pass
            
            try:
                SCREENSHOT_DIR.rmdir()
            except:
                pass
            
            print("✓")
            print(f"\n{'='*60}")
            print(f"✓ 完了！")
            print(f"  - PDFファイル: {output_pdf}")
            print(f"  - ページ数: {total_pages}ページ")
            if duplicates_removed > 0:
                print(f"  - 重複削除: {duplicates_removed}ページ")
            print(f"  - ファイルサイズ: {file_size:.2f} MB")
            print(f"  - PDF変換時間: {pdf_elapsed:.1f}秒")
            print(f"{'='*60}")
        else:
            print("\nエラー: PDFの作成に失敗しました。")
            print(f"一時スクリーンショットは {SCREENSHOT_DIR} に保存されています。")
    else:
        print("\nスクリーンショットが撮影されませんでした。")


if __name__ == "__main__":
    main()

