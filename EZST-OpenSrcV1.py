#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EZServerToolV1 OpenSource Edition Build Source
============

Minecraft (Spigot) サーバーのビルド・起動・管理を行う統合ツール。

旧 EZSERVERBUILDER.py / EZSERVERLAUNCHER.py を 1 つの実行ファイルに統合し、
サブコマンド (build / launch / cleanup) で機能を切り替える。

  EZServerToolV1.exe build       ... BuildTools で server.jar をビルドする
  EZServerToolV1.exe launch      ... サーバーを起動する (UPnP使用時は終了時に自動でポートを閉じる)
  EZServerToolV1.exe close-port  ... UPnP で開放したポートを単独で閉鎖する
  EZServerToolV1.exe cleanup     ... BuildTools が残した作業ファイルを削除する

引数なしでダブルクリック起動された場合は、対話式メニューを表示する。
"""

import argparse
import json
import platform
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import requests

try:
    import miniupnpc
    HAS_UPNP = True
except ImportError:
    HAS_UPNP = False


# ---------------------------------------------------------------------------
# パス解決
#
# exe化されると os.getcwd() はダブルクリック・ショートカット・管理者権限起動
# などで変動するため、実行ファイル自身の場所を基準にする。
# ---------------------------------------------------------------------------

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
CONFIG_FILE = BASE_DIR / "config.json"
BUILD_DIR = BASE_DIR / "build"
JAVA_DIR = BUILD_DIR / "java"
JAR_DIR = BUILD_DIR / "jar"
SERVER_DIR = BASE_DIR / "server"

JAVA_ZIP_URL = "https://cdn.azul.com/zulu/bin/zulu21.32.17-ca-jdk21.0.2-win_x64.zip"
JAVA_ZIP_NAME = "java17.zip"
JAVA_EXTRACTED_DIR_NAME = "zulu21.32.17-ca-jdk21.0.2-win_x64"
JAVA_EXEC = JAVA_DIR / JAVA_EXTRACTED_DIR_NAME / "bin" / "java.exe"

JAVA_VERSION_API_URL = "https://a36401948-ship-it.github.io/EZServerToolAPI/api/java-version.json"
AZUL_METADATA_API_URL = "https://api.azul.com/metadata/v1/zulu/packages/"

# 固定ビルド番号 (/200/) だと将来 Jenkins 側でアーティファクトが消えた時点で
# 全配布先が同時に壊れるため、常に最新の成功ビルドを指す lastSuccessfulBuild を使う。
BUILDTOOLS_URL = (
    "https://hub.spigotmc.org/jenkins/job/BuildTools/lastSuccessfulBuild/"
    "artifact/target/BuildTools.jar"
)
BUILDTOOLS_PATH = JAR_DIR / "BuildTools.jar"

DEFAULT_PORT = 25565

# cleanup で残すファイル / ディレクトリ。
# (BuildTools が cwd に展開する Bukkit/CraftBukkit/Spigot/work などのソース
#  ツリーを削除し、ツール本体・設定・成果物だけを残すためのホワイトリスト)
KEEP_NAMES = {"BuildTools.jar"}


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

def pause_and_exit(code: int = 0) -> None:
    """exe がダブルクリック起動された場合、コンソールが即座に閉じて
    エラーメッセージが読めなくなるのを防ぐ。"""
    try:
        input("\nEnterキーを押すと終了します...")
    except EOFError:
        pass
    sys.exit(code)


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        config = {
            "buildtools_downloaded": False,
            "java_downloaded": False,
            "java_extracted": False,
            "server_downloaded": False,
        }
        save_config(config)
        print("config.json を生成しました。初回インストールを実行します。")
        return config

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    print("config.json を読み込みました。")
    return config


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


def download_file(url: str, destination: Path, label: str) -> None:
    print(f"{label} をダウンロードします...")
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"✗ {label} のダウンロードに失敗しました: {e}")
        print("  ネットワーク接続、プロキシ、またはファイアウォール設定を確認してください。")
        raise
    ensure_directory(destination.parent)
    with open(destination, "wb") as f:
        f.write(response.content)
    print(f"✓ {label} のダウンロードが完了しました。")


def is_valid_version_string(ver: str) -> bool:
    """BuildTools の --rev に渡す値、および後段でファイル名の一部として
    扱う値なので、半角英数字と . - _ のみを許可する。"""
    if not ver:
        return False
    allowed = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
    )
    return all(c in allowed for c in ver)


def _version_key(value: str):
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", value.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def required_java_version(minecraft_version: str) -> int:
    """Read the compatibility table; Java 21 remains the offline fallback."""
    try:
        response = requests.get(JAVA_VERSION_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        default = int(data.get("default", 21))
        if minecraft_version in data.get("versions", {}):
            return int(data["versions"][minecraft_version])
        version = _version_key(minecraft_version)
        if version is not None:
            for item in data.get("ranges", []):
                lower, upper = _version_key(str(item["from"])), _version_key(str(item["to"]))
                if lower is not None and upper is not None:
                    length = max(len(version), len(lower), len(upper))
                    pad = lambda value: value + (0,) * (length - len(value))
                    if pad(lower) <= pad(version) <= pad(upper):
                        return int(item["java"])
        return default
    except (requests.RequestException, TypeError, ValueError, KeyError):
        print("Java compatibility API could not be read; using Java 21.")
        return 21


def resolve_java_download_url(java_version: int) -> str:
    system = platform.system().lower()
    os_name = "windows" if system == "windows" else "macos" if system == "darwin" else "linux"
    archive_type = "zip" if os_name == "windows" else "tar.gz"
    machine = platform.machine().lower()
    arches = ["arm"] if machine in ("arm64", "aarch64") else ["x64", "amd64"]
    for arch in arches:
        try:
            response = requests.get(AZUL_METADATA_API_URL, params={
                "java_version": str(java_version), "os": os_name, "arch": arch,
                "archive_type": archive_type, "java_package_type": "jdk",
                "javafx_bundled": "false", "release_status": "ga",
                "availability_types": "CA", "latest": "true", "page": "1", "page_size": "10",
            }, timeout=30)
            response.raise_for_status()
            for package in response.json():
                if package.get("java_version", [None])[0] == java_version:
                    return package["download_url"]
        except requests.RequestException:
            continue
    raise RuntimeError(f"Java {java_version} download URL could not be resolved for this platform.")


def find_java_executable(java_version: int) -> Path:
    executable = "java.exe" if platform.system() == "Windows" else "java"
    marker = f"jdk{java_version}"
    candidates = [path for path in JAVA_DIR.rglob(executable)
                  if path.parent.name == "bin" and marker in str(path).lower()]
    if not candidates:
        raise FileNotFoundError(f"Java {java_version} executable was not found in {JAVA_DIR}.")
    return min(candidates, key=lambda path: len(path.parts))


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"


def force_delete(path: Path) -> None:
    """shutil.rmtree / Path.unlink は権限エラー(読み取り専用属性・ファイル
    ロック等)で簡単に失敗するため、OSネイティブの削除コマンドに任せる。

      Windows      : ディレクトリ -> rd /s /q, ファイル -> del /f /q
      Linux/macOS  : rm -rf (ファイル・ディレクトリ共通)

    コマンドが失敗した場合は shutil 側の削除にフォールバックする。
    """
    path_str = str(path)

    if IS_WINDOWS:
        if path.is_dir():
            cmd = ["cmd", "/c", "rd", "/s", "/q", path_str]
        else:
            cmd = ["cmd", "/c", "del", "/f", "/q", path_str]
    else:
        cmd = ["rm", "-rf", path_str]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0 and path.exists():
        print(f"  ⚠ コマンドでの削除に失敗しました ({result.stderr.strip() or result.returncode})。")
        print("  Python標準の削除処理にフォールバックします...")
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except Exception as e:
            print(f"  ✗ 削除できませんでした: {e}")
            print("  ファイルが他のプロセスで使用中の可能性があります。")
            print("  サーバーやJavaプロセスを終了してから再試行してください。")


def cleanup(root: Path, dry_run: bool = False) -> None:
    root = root.resolve()
    if root == Path(root.anchor):
        raise ValueError("Refusing to clean the filesystem root.")

    if not root.exists():
        print(f"対象ディレクトリが存在しません: {root}")
        return

    for child in sorted(root.iterdir()):
        if child.name in KEEP_NAMES:
            print(f"keep:   {child}")
            continue
        print(f"delete: {child}")
        if not dry_run:
            force_delete(child)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def cmd_build() -> None:
    print("=" * 50)
    print("EZServerTool - BUILD モード")
    print("=" * 50)

    ensure_directory(JAVA_DIR)
    ensure_directory(JAR_DIR)
    ensure_directory(SERVER_DIR)

    config = load_config()

    # --- BuildTools.jar の取得 ---
    if not config.get("buildtools_downloaded", False):
        try:
            download_file(BUILDTOOLS_URL, BUILDTOOLS_PATH, "BuildTools.jar")
        except Exception:
            pause_and_exit(1)
        # ダウンロード（ファイル配置）が成功した後にフラグを立てる。
        config["buildtools_downloaded"] = True
        save_config(config)
    else:
        print("BuildTools.jar は既にダウンロード済みのためスキップします。")

    # The target version must be known before selecting a compatible JDK.
    requested_version = ""
    if not (config.get("server_downloaded", False) and (SERVER_DIR / "server.jar").exists()):
        while not is_valid_version_string(requested_version):
            requested_version = input("ビルドしたいバージョン (例: 1.21.4 / latest): ").strip()

    java_major = required_java_version(requested_version) if requested_version else 21
    if config.get("java_major") != java_major:
        config["java_downloaded"] = False
        config["java_extracted"] = False
    try:
        java_url = resolve_java_download_url(java_major)
    except Exception as e:
        print(f"✗ Java {java_major} のダウンロードURLを取得できませんでした: {e}")
        pause_and_exit(1)
        return
    zip_path = JAVA_DIR / java_url.rsplit("/", 1)[-1]

    # --- Java の取得・展開 ---
    if not config.get("java_downloaded", False):
        try:
            download_file(java_url, zip_path, f"Java (Zulu JDK {java_major})")
        except Exception:
            pause_and_exit(1)
        config["java_downloaded"] = True
        config["java_major"] = java_major
        save_config(config)
    else:
        print("Java は既にダウンロード済みのためスキップします。")

    if not config.get("java_extracted", False):
        print("Java を展開しています...")
        try:
            shutil.unpack_archive(str(zip_path), str(JAVA_DIR))
        except Exception as e:
            print(f"✗ Java の展開に失敗しました: {e}")
            pause_and_exit(1)
        config["java_extracted"] = True
        save_config(config)
        print("✓ Java の展開が完了しました。")
    else:
        print("Java は既に展開済みのためスキップします。")

    try:
        java_exec = find_java_executable(java_major)
    except FileNotFoundError as e:
        print(f"✗ {e}")
        print("  config.json の java_downloaded / java_extracted を false に")
        print("  戻してから再実行してください。")
        pause_and_exit(1)

    # --- Spigot のビルド ---
    server_jar = SERVER_DIR / "server.jar"
    if config.get("server_downloaded", False) and server_jar.exists():
        print("server.jar は既にビルド済みです。")
        print(f"再ビルドする場合は {server_jar} を削除してから実行してください。")
    else:
        ver = requested_version
        while not ver:
            ver = input(
                "ビルドしたいバージョンを半角英数字で入力してください "
                "(例: 1.21.4 / latest): "
            ).strip()
            if is_valid_version_string(ver):
                break
            print("✗ 無効な文字が含まれています。半角英数字・ . - _ のみ使用してください。")

        # BuildTools 実行前に既存の spigot-*.jar を記録しておく。
        # BuildTools はユーザー入力 (例: "latest") ではなく、解決後の実バージョン
        # 名でジャーを出力するため (例: spigot-1.21.4.jar)、入力値と出力ファイル名は
        # 必ずしも一致しない。そのため「実行前との差分」で出力ファイルを特定する。
        existing_jars = set(BASE_DIR.glob("spigot-*.jar"))

        print(f"BuildTools を実行します (--rev {ver}) ...")
        print("数分〜数十分かかる場合があります。ウィンドウを閉じずにお待ちください。")
        try:
            subprocess.run(
                [str(java_exec), "-jar", str(BUILDTOOLS_PATH), "--rev", ver],
                check=True,
                cwd=BASE_DIR / "build" / "jar",
            )
        except subprocess.CalledProcessError as e:
            print(f"✗ BuildTools の実行に失敗しました (終了コード {e.returncode})。")
            print("  入力したバージョン名が正しいか確認してください。")
            pause_and_exit(1)
        except FileNotFoundError:
            print("✗ Java または BuildTools.jar が見つかりません。")
            pause_and_exit(1)

        produced = sorted(
            set((BASE_DIR / "build" / "jar").glob("spigot-*.jar")) - existing_jars,
             key=lambda p: p.stat().st_mtime,
        )

        if not produced:
            print("✗ ビルド済みの spigot-*.jar が見つかりません。")
            print("  BuildTools の出力ログを確認してください。")
            pause_and_exit(1)

        built_jar = produced[-1]
        ensure_directory(SERVER_DIR)
        shutil.move(str(built_jar), str(server_jar))

        config["server_downloaded"] = True
        save_config(config)
        print(f"✓ ビルド完了: {built_jar.name} → {server_jar}")

    print("\nBuildTools が作業ディレクトリに残した一時ファイル "
          "(Bukkit / CraftBukkit / Spigot / work など) を削除しますか？")
    print("(build フォルダと server フォルダは保持されます)")
    ans = input("(y/n): ").strip().lower()
    if ans == "y":
        cleanup(BASE_DIR / "build" / "jar", dry_run=False)
    else:
        print("削除をスキップしました。")

    print("\nビルドが完了しました。")
    ans = input("続けてサーバーを起動しますか？ (y/n): ").strip().lower()
    if ans == "y":
        cmd_launch()


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------

def ensure_eula_agreed(eula_path: Path) -> None:
    lines = []
    if eula_path.exists():
        with open(eula_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    if any(line.strip().lower() == "eula=true" for line in lines):
        print("既に EULA に同意済みです。")
        return

    new_lines = []
    found = False
    for line in lines:
        if line.strip().lower().startswith("eula="):
            new_lines.append("eula=true\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        new_lines = lines + ["eula=true\n"]

    ensure_directory(eula_path.parent)
    with open(eula_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    print("EULA に自動で同意しました。")
    print("(起動時に Mojang EULA: https://www.minecraft.net/eula へ")
    print(" 自動的に同意したものとして扱われます)")


def setup_upnp_port_forward(port: int = DEFAULT_PORT):
    """UPnP でポート開放を設定し、外部 IP を取得する。失敗時は None を返す。"""
    print("\n[UPnP ポート開放]")

    if not HAS_UPNP:
        print("✗ miniupnpc モジュールが利用できません。")
        print("  ルーターの管理画面から手動でポート開放してください。")
        return None

    print("ルーターを検出中...")
    try:
        upnp = miniupnpc.UPnP()
        upnp.discoverdelay = 200
        discovered = upnp.discover()

        if discovered == 0:
            print("✗ UPnP 対応ルーターが見つかりません。")
            print("  ルーターの UPnP 機能が有効か確認してください。")
            return None

        upnp.selectigd()
        external_ip = upnp.externalipaddress()

        if external_ip == "0.0.0.0" or not external_ip:
            print("⚠ 外部 IP が取得できません。UPnP 設定を確認してください。")
            return None

        local_ip = getattr(upnp, "lanaddr", None) or socket.gethostbyname(
            socket.gethostname()
        )
        print("✓ ルーター検出成功")
        print(f"  外部 IP: {external_ip}")
        print(f"  内部 IP: {local_ip}")
        print(f"  ポート {port} を開放中...")

        try:
            existing_mapping = upnp.getspecificportmapping(port, "TCP")
            if existing_mapping:
                print(f"  既存の {port}/TCP マッピングが見つかりました。上書きします...")
                upnp.deleteportmapping(port, "TCP")
                print("  既存マッピングを削除しました。")
        except Exception:
            pass

        mapped = upnp.addportmapping(
            port, "TCP", local_ip, port, "Minecraft Server", ""
        )
        if not mapped:
            raise RuntimeError("ポートマッピングに失敗しました。")

        print("✓ ポート開放成功")
        print(f"\n接続先: {external_ip}:{port}")
        print("このアドレスを Minecraft クライアントのサーバーアドレス欄に入力してください。")
        print("\n⚠ 注意: ポートを開放すると、インターネット上の誰でもこのアドレスに")
        print("  接続を試行できる状態になります。server.properties の white-list や")
        print("  online-mode の設定を事前に確認することを強く推奨します。\n")
        return external_ip

    except Exception as e:
        print(f"✗ UPnP エラー: {e}")
        return None


def close_upnp_port_forward(port: int = DEFAULT_PORT) -> bool:
    """setup_upnp_port_forward で開いたポートマッピングを削除する。

    成功した場合、またはそもそも開いていなかった場合に True を返す。
    ルーターが見つからない・モジュールが無い等で確認できなかった場合は False。
    """
    print("\n[UPnP ポート閉鎖]")

    if not HAS_UPNP:
        print("✗ miniupnpc モジュールが利用できません。")
        print("  ルーターの管理画面から手動でポートを閉じてください。")
        return False

    print("ルーターを検出中...")
    try:
        upnp = miniupnpc.UPnP()
        upnp.discoverdelay = 200
        discovered = upnp.discover()

        if discovered == 0:
            print("✗ UPnP 対応ルーターが見つかりません。")
            print(f"  ポート {port}/TCP が開いている場合は手動で閉じてください。")
            return False

        upnp.selectigd()

        try:
            existing_mapping = upnp.getspecificportmapping(port, "TCP")
        except Exception:
            existing_mapping = None

        if not existing_mapping:
            print(f"ポート {port}/TCP は開放されていません。何もしません。")
            return True

        upnp.deleteportmapping(port, "TCP")
        print(f"✓ ポート {port}/TCP を閉鎖しました。")
        return True

    except Exception as e:
        print(f"✗ UPnP エラー: {e}")
        print(f"  ポート {port}/TCP が開いたままになっている可能性があります。")
        print("  ルーターの管理画面で確認することを推奨します。")
        return False


def launch_server(use_tunnel: bool) -> None:
    config = load_config()
    try:
        java_exec = find_java_executable(int(config.get("java_major", 21)))
    except (FileNotFoundError, TypeError, ValueError) as e:
        print(f"✗ Java 実行ファイルが見つかりません: {e}")
        print("  先に build を実行してください。")
        return

    server_jar = SERVER_DIR / "server.jar"
    if not server_jar.exists():
        print(f"✗ サーバージャーが見つかりません: {server_jar}")
        print("  先に build を実行してください。")
        return

    ensure_eula_agreed(SERVER_DIR / "eula.txt")

    if use_tunnel:
        external_ip = setup_upnp_port_forward()
        if not external_ip:
            print("✗ UPnP ポート開放に失敗しました。")
            print("  手動でポート開放するか、ローカルのみで起動してください。")
            return

    print("[サーバー起動]")
    print("サーバーを起動します... (停止するには Ctrl+C)")
    try:
        subprocess.run(
            [str(java_exec), "-jar", "server.jar", "nogui"],
            check=True,
            cwd=SERVER_DIR,
        )
    except KeyboardInterrupt:
        print("\nサーバーを停止しました。")
    except subprocess.CalledProcessError as e:
        print(f"✗ サーバーが異常終了しました (終了コード {e.returncode})。")
    except Exception as e:
        print(f"✗ サーバー起動エラー: {e}")
    finally:
        # 起動時にポートを開いた場合は、停止理由（正常終了・Ctrl+C・異常終了）
        # を問わず必ず閉じる。開けっぱなしのまま放置すると、サーバーが
        # 落ちている間もポートだけ外部に開いた状態が続いてしまうため。
        if use_tunnel:
            close_upnp_port_forward()


def cmd_launch() -> None:
    print("=" * 50)
    print("EZServerTool - LAUNCH モード")
    print("=" * 50)

    if not CONFIG_FILE.exists():
        print("config.json が存在しません。先に build を実行してください。")
        return

    config = load_config()
    required_keys = ("buildtools_downloaded", "java_downloaded", "server_downloaded")
    if not all(config.get(k, False) for k in required_keys):
        print("ビルドが完了していません。先に build を実行してください。")
        return

    print("ビルドが完了しています。サーバーを起動します。")

    while True:
        ans = input(
            "ポートを開放してインターネットに公開しますか？ "
            "公開する場合は y、ローカルのみで起動する場合は n を入力してください。(y/n): "
        ).strip().lower()
        if ans in ("y", "n"):
            break
        print("無効な入力です。y または n を入力してください。")

    launch_server(use_tunnel=(ans == "y"))


def cmd_close_port(port: int = DEFAULT_PORT) -> None:
    """サーバー起動中ではなく、単独でポートを閉じたい場合に使う。

    例: 前回 launch がクラッシュ等で異常終了し、finally のポート閉鎖が
    実行されなかった可能性がある場合の後始末。
    """
    print("=" * 50)
    print("EZServerTool - CLOSE-PORT モード")
    print("=" * 50)
    close_upnp_port_forward(port=port)


# ---------------------------------------------------------------------------
# 対話式メニュー (引数なしでダブルクリック起動された場合)
# ---------------------------------------------------------------------------

def interactive_menu() -> None:
    while True:
        print("\n" + "=" * 50)
        print("EZServerTool-OpenSourceEdition メインメニュー")
        print("git 1.0.0")
        print("=" * 50)
        print("1) サーバーをビルドする")
        print("2) サーバーを起動する")
        print("3) 開放したポートを閉じる")
        print("4) 作業ディレクトリをクリーンアップする")
        print("5) 終了する")
        choice = input("番号を選択してください (1-5): ").strip()

        if choice == "1":
            cmd_build()
        elif choice == "2":
            cmd_launch()
        elif choice == "3":
            cmd_close_port()
        elif choice == "4":
            dry_ans = input("実際には削除せず一覧表示のみ行いますか？ (y/n): ").strip().lower()
            cleanup(BASE_DIR, dry_run=(dry_ans == "y"))
        elif choice == "5":
            break
        else:
            print("無効な入力です。1〜5の番号を入力してください。")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="EZServerTool",
        description="EZServerTool - Minecraft (Spigot) サーバーのビルド・起動・管理ツール",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("build", help="BuildTools で server.jar をビルドします。")
    sub.add_parser("launch", help="サーバーを起動します。")

    close_port_parser = sub.add_parser(
        "close-port", help="UPnP で開放したポートを閉鎖します。"
    )
    close_port_parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"閉鎖するポート番号 (既定: {DEFAULT_PORT})"
    )

    cleanup_parser = sub.add_parser(
        "cleanup", help="BuildTools が残した作業ファイルを削除します。"
    )
    cleanup_parser.add_argument(
        "--dry-run", action="store_true", help="削除せず一覧表示のみ行います。"
    )
    cleanup_parser.add_argument(
        "--root", default=str(BASE_DIR), help="掃除対象のルートディレクトリ。"
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.command == "build":
        cmd_build()
    elif args.command == "launch":
        cmd_launch()
    elif args.command == "close-port":
        cmd_close_port(port=args.port)
    elif args.command == "cleanup":
        cleanup(Path(args.root), dry_run=args.dry_run)
    else:
        # サブコマンドなし = ダブルクリック起動の可能性が高いため、
        # 何も起きずに終了するのではなく対話式メニューを表示する。
        interactive_menu()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        # --help / -h など argparse が正常に発生させる終了はそのまま通す。
        raise
    except KeyboardInterrupt:
        print("\n中断されました。")
        pause_and_exit(0)
    except Exception as e:
        print(f"\n✗ 予期しないエラーが発生しました: {e}")
        pause_and_exit(1)
    else:
        pause_and_exit(0)
