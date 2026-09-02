#!/usr/bin/env python3
"""
Publish torch-gpu-linux to Maven Central using Sonatype Central Portal API.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
import ssl
import requests

ssl_ctx = ssl._create_unverified_context()

# Config
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STAGE = Path(os.environ.get("STAGE_DIR", SCRIPT_DIR / "staging"))
DEFAULT_BUNDLE = Path(os.environ.get("BUNDLE_DIR", SCRIPT_DIR / "bundles"))

GROUP_ID = "io.github.mullerhai"
GROUP_PATH = GROUP_ID.replace(".", "/")
ARTIFACT_ID = "torch-gpu-linux"
VERSION = "13.3-9.25-1.5.14-GA-1.10"
CLASSIFIER = ""  # Empty for parent POM (packaging=pom)
PACKAGING = "pom"
POM_ONLY = True  # When True, no stub sources/javadoc jars are generated

PROJECT_URL = "https://github.com/mullerhai/torch-gpu-linux"
SCM_URL = "https://github.com/mullerhai/torch-gpu-linux"
SCM_CONN = "scm:git:git://github.com/mullerhai/torch-gpu-linux.git"
SCM_DEV = "scm:git:ssh://git@github.com:mullerhai/torch-gpu-linux.git"
LICENSE_NAME = "Apache License, Version 2.0"
LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0"
DEV_NAME = "Muller Hai"
DEV_EMAIL = "hai710459649@foxmail.com"
DEV_URL = "https://github.com/mullerhai"
ORG_NAME = "mullerhai"
DEV_ID = "mullerhai"

CENTRAL_UPLOAD = "https://central.sonatype.com/api/v1/publisher/upload"
CENTRAL_STATUS = "https://central.sonatype.com/api/v1/publisher/status"
CENTRAL_PUBLISH = "https://central.sonatype.com/api/v1/publisher/deployment"


def log(msg: str) -> None:
    print(msg, flush=True)


def sha_digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(path: Path) -> None:
    for algo, ext in (("md5", ".md5"), ("sha1", ".sha1"), ("sha256", ".sha256"), ("sha512", ".sha512")):
        (path.parent / (path.name + ext)).write_text(sha_digest(path, algo) + "\n", encoding="ascii")


def gpg_sign(path: Path, key_id: str = "C908541CBE90F9F460D4039DF46B9492FFC59C9A") -> Path:
    sig = path.with_suffix(path.suffix + ".asc")
    if sig.exists():
        sig.unlink()
    env = os.environ.copy()
    env["GNUPGHOME"] = "/tmp/gnupg-publish"
    cmd = [
        "gpg",
        "--homedir", env["GNUPGHOME"],
        "--batch",
        "--yes",
        "--local-user",
        key_id,
        "--detach-sign",
        "--armor",
        "--output",
        str(sig),
        str(path),
    ]
    if env.get("GPG_PASSPHRASE"):
        cmd.extend(["--pinentry-mode", "loopback", "--passphrase-fd", "0"])
        subprocess.run(cmd, input=env["GPG_PASSPHRASE"] + "\n", text=True, check=True, env=env)
    else:
        subprocess.run(cmd, check=True, env=env)
    return sig


def build_pom(version: str | None = None) -> str:
    v = version or VERSION
    classifier_line = f"  <classifier>{CLASSIFIER}</classifier>\n" if CLASSIFIER else ""
    packaging_line = f"  <packaging>{PACKAGING}</packaging>\n" if PACKAGING and PACKAGING != "jar" else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{GROUP_ID}</groupId>
  <artifactId>{ARTIFACT_ID}</artifactId>
  <version>{v}</version>
{classifier_line}{packaging_line}  <name>{ARTIFACT_ID}</name>
  <description>PyTorch GPU Linux distribution with CUDA support</description>
  <url>{PROJECT_URL}</url>
  <licenses>
    <license>
      <name>{LICENSE_NAME}</name>
      <url>{LICENSE_URL}</url>
      <distribution>repo</distribution>
    </license>
  </licenses>
  <developers>
    <developer>
      <id>{DEV_ID}</id>
      <name>{DEV_NAME}</name>
      <email>{DEV_EMAIL}</email>
      <url>{DEV_URL}</url>
      <organization>{ORG_NAME}</organization>
      <organizationUrl>{DEV_URL}</organizationUrl>
    </developer>
  </developers>
  <scm>
    <url>{SCM_URL}</url>
    <connection>{SCM_CONN}</connection>
    <developerConnection>{SCM_DEV}</developerConnection>
  </scm>
</project>
"""


def main_jar_filename(ver: str, classifier: str | None = None) -> str:
    """Return the canonical Maven filename for the primary artifact."""
    c = classifier if classifier is not None else CLASSIFIER
    base = f"{ARTIFACT_ID}-{ver}"
    return f"{base}-{c}.jar" if c else f"{base}.jar"


def minimal_sources_jar(out: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "README-sources.txt",
            f"{ARTIFACT_ID} {VERSION}\nSources not bundled for this republished artifact.\nSee {PROJECT_URL}\n",
        )
        zf.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\nCreated-By: mullerhai-publish\n\n",
        )
    out.write_bytes(buf.getvalue())


def minimal_javadoc_jar(out: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        readme = (
            f"{ARTIFACT_ID} {VERSION}\n"
            f"Javadoc not generated for this platform-native / republished artifact.\n"
            f"See {PROJECT_URL}\n"
        )
        zf.writestr("README-javadoc.txt", readme)
        zf.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\nCreated-By: mullerhai-publish\n\n",
        )
    out.write_bytes(buf.getvalue())


def stage_artifact(stage: Path, source_jar: Path, version: str | None = None) -> list[Path]:
    ver = version or VERSION
    out_dir = stage / GROUP_PATH / ARTIFACT_ID / ver
    out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []

    # POM (always written from build_pom with the active VERSION, see caller)
    pom_out = out_dir / f"{ARTIFACT_ID}-{ver}.pom"
    pom_out.write_text(build_pom(version=ver), encoding="utf-8")
    produced.append(pom_out)

    # For POM-only artifacts (parent POM, packaging=pom), skip jar/sources/javadoc.
    if POM_ONLY:
        log(f"  staged {ARTIFACT_ID}:{ver} (POM-only) -> {out_dir} ({len(produced)} files)")
        return produced

    # Main jar (copy from source, filename includes classifier when set)
    main_out = out_dir / main_jar_filename(ver)
    if source_jar.exists() and source_jar.stat().st_size > 0:
        shutil.copy2(source_jar, main_out)
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "README.txt",
                f"{ARTIFACT_ID} {ver}\nNo binary artifact for this republished POM-only module.\nSee {PROJECT_URL}\n",
            )
            zf.writestr(
                "META-INF/MANIFEST.MF",
                "Manifest-Version: 1.0\nCreated-By: mullerhai-publish\n\n",
            )
        main_out.write_bytes(buf.getvalue())
    produced.append(main_out)

    # sources
    sources_out = out_dir / f"{ARTIFACT_ID}-{ver}-sources.jar"
    minimal_sources_jar(sources_out)
    produced.append(sources_out)

    # javadoc
    javadoc_out = out_dir / f"{ARTIFACT_ID}-{ver}-javadoc.jar"
    minimal_javadoc_jar(javadoc_out)
    produced.append(javadoc_out)

    log(f"  staged {ARTIFACT_ID}:{ver} (classifier={CLASSIFIER}) -> {out_dir} ({len(produced)} files)")
    return produced


def stage_from_m2(stage: Path, m2_dir: Path, version: str) -> list[Path]:
    """Stage directly from an existing ~/.m2/repository/.../<version>/ directory.

    Copies EVERY .jar plus the .pom from m2 into a fresh staging dir, rewriting
    the embedded version segment (e.g. `1.01`) to `version`. Jar filenames are
    the canonical Maven layout: `${artifactId}-${version}[-classifier].jar`.
    Strips any *.sha1 / *.sha256 etc. sidecar files (they get regenerated).
    """
    if not m2_dir.is_dir():
        raise SystemExit(f"--from-m2 path is not a directory: {m2_dir}")

    out_dir = stage / GROUP_PATH / ARTIFACT_ID / version
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    pom_in = m2_dir / f"{ARTIFACT_ID}-{VERSION}.pom"
    if not pom_in.exists():
        raise SystemExit(f"--from-m2 missing pom: {pom_in}")
    pom_out = out_dir / f"{ARTIFACT_ID}-{version}.pom"
    shutil.copy2(pom_in, pom_out)

    produced: list[Path] = [pom_out]

    # POM-only artifacts stop here (no jar / sources / javadoc).
    if POM_ONLY:
        log(f"  staged {ARTIFACT_ID}:{version} (POM-only, from m2) -> {out_dir} ({len(produced)} files)")
        return produced

    # Copy every jar present. Special handling for variants:
    #   * `-linux-x86_64-gpu.jar`   → copy as-is with version rename
    #   * `-linux-x86_64.jar`       → copy as-is with version rename
    #   * `-sources.jar`            → copy as-is with version rename
    #   * `-javadoc.jar`            → copy as-is with version rename
    #   * plain `.jar` (no classifier) → copy with version rename
    jars = sorted(p for p in m2_dir.glob(f"{ARTIFACT_ID}-{VERSION}*.jar"))
    if not jars:
        # Fallback: m2 dir may have a templates-only `pytorch-javadoc.jar` (no version).
        jars = sorted(p for p in m2_dir.glob(f"{ARTIFACT_ID}*.jar"))

    for src in jars:
        # Compute destination filename: replace the first occurrence of source VERSION with new VERSION
        new_name = src.name
        if VERSION in new_name:
            new_name = new_name.replace(VERSION, version, 1)
        # If the filename has no version segment at all (rare; e.g. pytorch-javadoc.jar),
        # inject the version + canonical -javadoc classifier per Maven layout.
        elif new_name == f"{ARTIFACT_ID}-javadoc.jar":
            new_name = f"{ARTIFACT_ID}-{version}-javadoc.jar"
        dst = out_dir / new_name
        shutil.copy2(src, dst)
        produced.append(dst)
        log(f"    {src.name} -> {dst.name}")

    # Ensure sources + javadoc always exist. The pytorch-javadoc.jar under m2
    # usually lacks a version segment and is intended as a template, so we only
    # treat it as the real javadoc if it has a reasonable size.
    sources_out = out_dir / f"{ARTIFACT_ID}-{version}-sources.jar"
    if not sources_out.exists() or sources_out.stat().st_size == 0:
        minimal_sources_jar(sources_out)
        produced.append(sources_out)

    javadoc_out = out_dir / f"{ARTIFACT_ID}-{version}-javadoc.jar"
    needs_minimal = not javadoc_out.exists() or javadoc_out.stat().st_size < 4096
    if needs_minimal:
        minimal_javadoc_jar(javadoc_out)
        produced.append(javadoc_out)

    log(f"  staged {ARTIFACT_ID}:{version} -> {out_dir} ({len(produced)} files)")
    return produced


def sign_all(stage: Path, skip: bool = False) -> None:
    """Sign primary artifacts and emit checksum sidecars.

    Maven Central requirements:
      * Every `.jar` / `.pom` MUST be accompanied by `.md5` and `.sha1`
        checksum files (`.sha256` / `.sha512` are optional).
      * Every `.jar` / `.pom` MUST have a detached GPG `.asc` signature.
      * `.asc` signature files MUST NOT carry checksum sidecars (Central
        Portal explicitly rejects `.jar.asc.sha1`, `.jar.asc.md5`, etc.)
      * Checksum files MUST NOT be `.asc`-signed (Central rejects them too).
    """
    if skip:
        log("Skipping GPG signing (--no-sign flag)")
        return

    sig_ext = (".md5", ".sha1", ".sha256", ".sha512")
    # Step 1: clean up any leftover sidecar files under the staging tree. This
    # ensures a previous run's `.jar.asc.sha1` etc. are gone before we regen.
    for p in stage.rglob("*"):
        if not p.is_file():
            continue
        if p.name.endswith(sig_ext) and any(p.name.endswith(e + ".asc") for e in sig_ext):
            p.unlink()

    primary = sorted(
        p for p in stage.rglob("*")
        if p.is_file()
        and not p.name.endswith(".asc")
        and not p.name.endswith(sig_ext)
    )

    for p in primary:
        log(f"  sign {p.relative_to(stage)}")
        write_checksums(p)   # .md5 / .sha1 / .sha256 / .sha512
        gpg_sign(p)          # .asc (no checksum sidecars!)

    log("Sign complete.")


def bundle_all(stage: Path, bundle_dir: Path, version: str | None = None) -> Path:
    ver = version or VERSION
    bundle_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name_suffix = f"-{CLASSIFIER}" if CLASSIFIER else ""
    zip_path = bundle_dir / f"{ARTIFACT_ID}-{ver}{name_suffix}-{stamp}.zip"
    if zip_path.exists():
        zip_path.unlink()

    count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(stage.rglob("*")):
            if not p.is_file():
                continue
            arc = p.relative_to(stage).as_posix()
            zf.write(p, arcname=arc)
            count += 1
    log(f"Bundle: {zip_path} ({count} files, {zip_path.stat().st_size / (1<<20):.1f} MiB)")
    return zip_path


def get_credentials() -> tuple:
    user = os.environ.get("CENTRAL_USERNAME") or os.environ.get("SONATYPE_USERNAME")
    pwd = os.environ.get("CENTRAL_PASSWORD") or os.environ.get("SONATYPE_PASSWORD")
    if not user or not pwd:
        settings = Path.home() / ".m2" / "settings.xml"
        if settings.exists():
            try:
                tree = ET.parse(settings)
                for server in tree.getroot().iter():
                    if server.tag.endswith("server"):
                        sid = None
                        for child in server:
                            if child.tag.endswith("id"):
                                sid = child.text
                            if child.tag.endswith("username"):
                                user = child.text
                            if child.tag.endswith("password"):
                                pwd = child.text
                        if sid == "ossrh" or sid == "central":
                            break
            except Exception as e:
                log(f"warn: could not parse settings.xml: {e}")
    if not user or not pwd:
        raise SystemExit(
            "Missing Central credentials. Set CENTRAL_USERNAME and CENTRAL_PASSWORD "
            "or put them in ~/.m2/settings.xml under <server><id>ossrh</id>."
        )
    return user, pwd


def upload_bundle(zip_path: Path, publishing_type: str = "USER_MANAGED", version: str | None = None) -> str:
    user, pwd = get_credentials()
    ver = version or VERSION
    file_size = zip_path.stat().st_size
    log(f"Uploading {zip_path.name} ({file_size/(1<<20):.1f} MiB) to Central Portal ...")

    name_part = f"{ARTIFACT_ID}-{ver}-{CLASSIFIER}" if CLASSIFIER else f"{ARTIFACT_ID}-{ver}"
    url = (
        f"{CENTRAL_UPLOAD}"
        f"?publishingType={publishing_type}"
        f"&name={name_part}"
    )

    # Use requests with streaming to avoid multipart timeout on large files
    MAX_RETRIES = 3
    RETRY_DELAY = 30

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"  [upload] attempt {attempt}/{MAX_RETRIES}")
        try:
            with open(zip_path, "rb") as f:
                files = {"bundle": (zip_path.name, f, "application/zip")}
                resp = requests.post(
                    url,
                    files=files,
                    auth=(user, pwd),
                    timeout=(300, 86400),  # (connect, read)
                    stream=True,
                )
            break
        except requests.exceptions.Timeout as e:
            log(f"  [upload] attempt {attempt} timed out: {e}")
            if attempt < MAX_RETRIES:
                log(f"  sleeping {RETRY_DELAY}s before retry")
                time.sleep(RETRY_DELAY)
            else:
                raise
        except Exception as e:
            log(f"  [upload] attempt {attempt} error: {e}")
            if attempt < MAX_RETRIES:
                log(f"  sleeping {RETRY_DELAY}s before retry")
                time.sleep(RETRY_DELAY)
            else:
                raise

    body_text = resp.text.strip()
    status_code = resp.status_code
    log(f"  upload completed, HTTP {status_code}, response: {body_text[:300]}")

    if status_code not in (200, 201, 202):
        raise SystemExit(f"Upload failed HTTP {status_code}: {body_text[:500]}")

    # Parse deploymentId from JSON response
    try:
        data = resp.json()
        deployment_id = data.get("deploymentId", "")
    except Exception:
        deployment_id = body_text

    if not deployment_id or "{" in deployment_id:
        raise SystemExit(f"Upload failed: no deploymentId in response: {body_text[:500]}")

    log(f"Upload OK. deploymentId = {deployment_id}")
    return deployment_id


def poll_status(deployment_id: str, timeout_s: int = 1800) -> dict:
    user, pwd = get_credentials()
    token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
    url = f"{CENTRAL_STATUS}?id={deployment_id}"
    body_path = Path(tempfile.gettempdir()) / f"central_status_{os.getpid()}.json"

    start = time.time()
    while True:
        cmd = [
            "curl",
            "-sS",
            "-X",
            "POST",
            "--http1.1",
            "-H",
            f"Authorization: Bearer {token}",
            url,
            "-o",
            str(body_path),
            "-w",
            "%{http_code}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        raw = body_path.read_text(encoding="utf-8", errors="replace") if body_path.exists() else ""
        body_path.unlink(missing_ok=True)

        http_code = (proc.stdout or "").strip()
        if http_code not in ("200", "201", "202", "204"):
            log(f"  Status check HTTP {http_code}: {raw[:200]}")
            time.sleep(15)
            continue

        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"state": "?"}

        state = data.get("deploymentState") or data.get("state") or "?"
        log(f"  deployment {deployment_id}: {state}")
        if state in ("PUBLISHED", "FAILED", "VALIDATED"):
            return data
        if time.time() - start > timeout_s:
            log("Timeout waiting for deployment; check https://central.sonatype.com/publishing")
            return data
        time.sleep(15)


def publish_deployment(deployment_id: str) -> None:
    user, pwd = get_credentials()
    token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
    url = f"{CENTRAL_PUBLISH}/{deployment_id}"
    body_path = Path(tempfile.gettempdir()) / f"central_publish_{os.getpid()}.json"
    cmd = [
        "curl",
        "-sS",
        "-X",
        "POST",
        "--http1.1",
        "-H",
        f"Authorization: Bearer {token}",
        url,
        "-o",
        str(body_path),
        "-w",
        "%{http_code}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    body_path.unlink(missing_ok=True)
    log(f"Publish requested for {deployment_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish pytorch to Maven Central")
    parser.add_argument("--stage-dir", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--source-jar", type=Path, default=None)
    parser.add_argument("--from-m2", type=Path, default=None,
                        help="Stage directly from this m2 directory (e.g. ~/.m2/repository/io/.../<ver>)")
    parser.add_argument("--version", default=None,
                        help="Override VERSION for this run (e.g. 2.13.0-1.5.14-GA-1.10)")
    parser.add_argument("--upload", action="store_true", help="Upload after staging and signing")
    parser.add_argument("--publishing-type", choices=["USER_MANAGED", "AUTOMATIC"], default="USER_MANAGED")
    parser.add_argument("--no-wait", action="store_true", help="Upload and return immediately")
    parser.add_argument("--publish", action="store_true", help="Call publish API after upload")
    parser.add_argument("--no-sign", action="store_true", help="Skip GPG signing (for manual signing)")
    args = parser.parse_args()

    log(f"""
============================================================
  {ARTIFACT_ID} -> Maven Central
============================================================
  groupId    : {GROUP_ID}
  artifactId : {ARTIFACT_ID}
  version    : {args.version or VERSION}
  classifier : {CLASSIFIER or '(none)'}
  packaging  : {PACKAGING}
  pom-only   : {POM_ONLY}
  source jar : {args.source_jar or 'none'}
============================================================
""")

    # Stage
    stage = args.stage_dir
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    # Belt-and-suspenders: remove any stale version subdir left behind by rmtree failures
    stale_version_dir = stage / GROUP_PATH / ARTIFACT_ID / VERSION
    if stale_version_dir.exists():
        shutil.rmtree(stale_version_dir, ignore_errors=True)

    source_jar = args.source_jar
    if not source_jar:
        default_jar = Path("target") / f"{ARTIFACT_ID}-{VERSION}.jar"
        if default_jar.exists():
            source_jar = default_jar
        else:
            default_jar = Path(__file__).parent / "target" / f"{ARTIFACT_ID}-{VERSION}.jar"
            if default_jar.exists():
                source_jar = default_jar

    log(f"Staging into {stage}")
    active_version = args.version or VERSION
    if args.from_m2:
        stage_from_m2(stage, args.from_m2, active_version)
    else:
        stage_artifact(stage, source_jar or Path("/dev/null"), version=active_version)

    # Sign
    log(f"Signing artifacts under {stage}")
    sign_all(stage, skip=args.no_sign)

    # Bundle
    log(f"Bundling into {args.bundle_dir}")
    zip_path = bundle_all(stage, args.bundle_dir, version=active_version)

    if args.upload:
        dep_id = upload_bundle(zip_path, publishing_type=args.publishing_type, version=active_version)
        if args.no_wait:
            log(f"Upload submitted. deploymentId={dep_id}")
            log("Review: https://central.sonatype.com/publishing/deployments")
            return 0
        data = poll_status(dep_id)
        if args.publish and data.get("deploymentState") == "VALIDATED":
            publish_deployment(dep_id)
            poll_status(dep_id)
        log(f"Done. deploymentId={dep_id}")
        log("Review: https://central.sonatype.com/publishing/deployments")
    else:
        log(f"Bundle ready: {zip_path}")
        log("Set CENTRAL_USERNAME/CENTRAL_PASSWORD then re-run with --upload")

    return 0


if __name__ == "__main__":
    sys.exit(main())
