#!/usr/bin/env python3
"""Guard, patch and script-sync tool for the ad-free 3x-ui fork.

Subcommands:
  check    Compare the upstream tree with the pinned ad baseline.
  apply    Remove the sponsor/ad surface and repoint self-update URLs.
  scripts  Copy upstream install/update/CLI scripts with fork URLs baked in.

check exit codes: 0 = patchable, 3 = skip (reason in the JSON report),
4 = hard error (bad baseline, unreadable tree).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BASELINE = SCRIPT_DIR.parent / "baseline.json"
FORK_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

SCRIPT_FILES = (
    "install.sh",
    "update.sh",
    "x-ui.sh",
    "x-ui.rc",
    "x-ui.service.debian",
    "x-ui.service.arch",
    "x-ui.service.rhel",
)

SPONSOR_STUB = '''package panel

import (
\t"errors"
\t"time"
)

// Sponsor is one paid placement record (kept for API compatibility).
type Sponsor struct {
\tID     string            `json:"id" example:"acme-2026-10"`
\tName   string            `json:"name" example:"Acme VPS"`
\tEnable *bool             `json:"enable,omitempty" example:"true"`
\tSlots  []string          `json:"slots"`
\tFrom   *time.Time        `json:"from,omitempty" example:"2026-10-01T00:00:00Z"`
\tUntil  time.Time         `json:"until" example:"2026-11-01T00:00:00Z"`
\tLogo   string            `json:"logo,omitempty" example:"/sponsors/logo/acme.png"`
\tTitle  map[string]string `json:"title"`
\tText   map[string]string `json:"text"`
\tLink   string            `json:"link" example:"https://acme.example/?utm_source=3x-ui"`
}

// SponsorList is the active sponsor set plus the contact link for new sponsors.
type SponsorList struct {
\tContact  string    `json:"contact,omitempty" example:"https://t.me/example"`
\tSponsors []Sponsor `json:"sponsors"`
}

// ErrSponsorLogoUnknown rejects logo names not used by an active sponsor.
var ErrSponsorLogoUnknown = errors.New("unknown sponsor logo")

// GetSponsors always returns an empty list: this build carries no sponsor feed
// and never contacts the upstream sponsors repository.
func (s *PanelService) GetSponsors() (*SponsorList, error) {
\treturn &SponsorList{Sponsors: []Sponsor{}}, nil
}

// GetSponsorLogo always fails because GetSponsors never exposes a logo.
func (s *PanelService) GetSponsorLogo(name string) ([]byte, string, error) {
\treturn nil, "", ErrSponsorLogoUnknown
}
'''


class PatchError(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def load_baseline(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PatchError(f"cannot read baseline {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PatchError(f"invalid baseline JSON {path}: {exc}") from exc
    if not isinstance(data.get("ad_files"), dict) or not data["ad_files"]:
        raise PatchError("baseline is missing a non-empty ad_files map")
    return data


def validate_fork(value: str) -> str:
    value = value.strip()
    if not FORK_RE.match(value):
        raise PatchError(f"invalid fork repo '{value}', expected OWNER/REPO")
    return value


def iter_source_files(src: Path):
    for base in ("internal/web", "frontend/src"):
        root = src / base
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in (".go", ".ts", ".tsx"):
                yield path


def ad_markers(src: Path) -> list[str]:
    hits = []
    for path in iter_source_files(src):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "sponsors.json" in text or "SponsorSlot" in text or "/sponsors" in text:
            hits.append(str(path.relative_to(src)))
    return hits


def run_check(src: Path, baseline: dict) -> dict:
    report = {
        "status": "ok",
        "kind": "ok",
        "reason": "upstream ad code matches the pinned baseline; patch can be applied",
        "changed": [],
        "missing": [],
        "anchor_failures": [],
    }
    ad_files = baseline["ad_files"]
    for rel, expected in ad_files.items():
        path = src / rel
        if not path.is_file():
            report["missing"].append(rel)
            continue
        actual = sha256_file(path)
        if actual != expected:
            report["changed"].append(f"{rel}: baseline {expected[:12]}, upstream {actual[:12]}")

    markers = ad_markers(src)
    if not markers:
        report.update(
            status="skip",
            kind="removed",
            reason=(
                "upstream no longer contains advertisement code "
                "(no sponsor markers found); nothing to patch, skipping build"
            ),
        )
        return report

    if report["missing"] or report["changed"]:
        details = report["missing"] + report["changed"]
        report.update(
            status="skip",
            kind="changed",
            reason=(
                "upstream advertisement code changed and the patch is not proven "
                "compatible; skipping build. Details: " + "; ".join(details)
            ),
        )
        return report

    for anchor in baseline.get("anchors", []):
        path = src / anchor["path"]
        if not path.is_file():
            report["anchor_failures"].append(f"{anchor['path']}: file missing")
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in anchor["contains"]:
            if needle not in text:
                report["anchor_failures"].append(f"{anchor['path']}: missing {needle!r}")

    if report["anchor_failures"]:
        report.update(
            status="skip",
            kind="changed",
            reason=(
                "upstream ad mount points or self-update endpoints changed; skipping "
                "build. Details: " + "; ".join(report["anchor_failures"])
            ),
        )
    return report


def sub_once(path: Path, pattern: str, repl: str, label: str, flags: int = re.MULTILINE) -> None:
    text = path.read_text(encoding="utf-8")
    matches = re.findall(pattern, text, flags)
    if len(matches) != 1:
        raise PatchError(f"{label}: expected exactly 1 match in {path}, found {len(matches)}")
    updated = re.sub(pattern, lambda _m: repl, text, count=1, flags=flags)
    path.write_text(updated, encoding="utf-8")


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected exactly 1 occurrence in {path}, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def apply_patch(src: Path, baseline: dict, fork: str) -> None:
    report = run_check(src, baseline)
    if report["status"] != "ok":
        raise PatchError(f"refusing to patch: {report['reason']}")

    # 1. Replace the sponsor service with a no-feed stub. Public types and the
    #    two routes stay so the generated OpenAPI contract (/sponsors) survives.
    sponsor_go = src / "internal/web/service/panel/sponsor.go"
    sponsor_go.write_text(SPONSOR_STUB, encoding="utf-8")
    test_file = src / "internal/web/service/panel/sponsor_test.go"
    if test_file.exists():
        test_file.unlink()

    # 2. Remove the visible ad mounts / navigation entry points. Since v3.11.0
    #    the router, sidebar, page titles and command palette all read one
    #    route manifest, so deleting its sponsor row removes all four at once.
    sub_once(
        src / "frontend/src/routes/panelRoutes.tsx",
        r"^[ \t]*CrownOutlined,\n",
        "",
        "panelRoutes CrownOutlined import",
    )
    sub_once(
        src / "frontend/src/routes/panelRoutes.tsx",
        r"^const SponsorsPage = lazy\(\(\) => import\('@/pages/sponsors/SponsorsPage'\)\);\n",
        "",
        "panelRoutes lazy import",
    )
    sub_once(
        src / "frontend/src/routes/panelRoutes.tsx",
        r"^[ \t]*\{ path: '/sponsors',[^\n]*\n",
        "",
        "panelRoutes sponsors entry",
    )
    sub_once(
        src / "frontend/src/pages/login/LoginPage.tsx",
        r"^import SponsorSlot from '@/components/sponsor/SponsorSlot';\n",
        "",
        "LoginPage import",
    )
    sub_once(
        src / "frontend/src/pages/login/LoginPage.tsx",
        r"^[ \t]*<SponsorSlot slot=\"login\"[^\n]*\n",
        "",
        "LoginPage mount",
    )
    sub_once(
        src / "frontend/src/pages/index/IndexPage.tsx",
        r"^import SponsorSlot from '@/components/sponsor/SponsorSlot';\n",
        "",
        "IndexPage import",
    )
    sub_once(
        src / "frontend/src/pages/index/IndexPage.tsx",
        r"^[ \t]*<SponsorSlot slot=\"dashboard\"[^\n]*\n",
        "",
        "IndexPage mount",
    )
    sub_once(
        src / "frontend/src/components/command-palette/CommandPalette.tsx",
        r"^[ \t]*'/sponsors':[^\n]*\n",
        "",
        "command palette sponsor keywords",
    )

    # The manifest was the only remaining user of menu.sponsors; drop the now
    # dead key from every locale so the i18n dead-key test stays green.
    for locale in sorted((src / "internal/web/translation").glob("*.json")):
        data = json.loads(locale.read_text(encoding="utf-8"))
        menu = data.get("menu")
        if isinstance(menu, dict):
            menu.pop("sponsors", None)
        locale.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # 3. Repoint the panel self-update channel and the source link at the fork.
    panel_go = src / "internal/web/service/panel/panel.go"
    replace_once(
        panel_go,
        "https://raw.githubusercontent.com/MHSanaei/3x-ui/main/update.sh",
        f"https://raw.githubusercontent.com/{fork}/main/update.sh",
        "panel updater URL",
    )
    replace_once(
        panel_go,
        "https://api.github.com/repos/MHSanaei/3x-ui/releases/latest",
        f"https://api.github.com/repos/{fork}/releases/latest",
        "panel release API",
    )
    replace_once(
        panel_go,
        "https://api.github.com/repos/MHSanaei/3x-ui/releases/tags/",
        f"https://api.github.com/repos/{fork}/releases/tags/",
        "panel release tag API",
    )
    replace_once(
        src / "frontend/src/layouts/AppSidebar.tsx",
        "const REPO_URL = 'https://github.com/MHSanaei/3x-ui';",
        f"const REPO_URL = 'https://github.com/{fork}';",
        "AppSidebar repo link",
    )

    # 4. Post-conditions: no ad feed, no ad mounts, no upstream update channel.
    leftovers = []
    for path in iter_source_files(src):
        rel = path.relative_to(src)
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in (
            "<SponsorSlot",
            "raw.githubusercontent.com/MHSanaei/3x-ui",
            "api.github.com/repos/MHSanaei/3x-ui",
            "MHSanaei/sponsors",
            "menu.sponsors",
        ):
            if needle in text:
                leftovers.append(f"{rel}: {needle}")
        if str(rel).startswith("internal/web/") and "sponsors.json" in text:
            leftovers.append(f"{rel}: sponsors.json")
    if leftovers:
        raise PatchError("patch left forbidden ad/update references: " + "; ".join(leftovers))


def generate_scripts(src: Path, dest: Path, fork: str) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name in SCRIPT_FILES:
        source = src / name
        if not source.is_file():
            raise PatchError(f"upstream is missing required script {name}")
        text = source.read_text(encoding="utf-8")
        text = text.replace("MHSanaei/3x-ui", fork).replace("mhsanaei/3x-ui", fork)
        for stale in ("MHSanaei/3x-ui", "mhsanaei/3x-ui"):
            if stale in text:
                raise PatchError(f"{name} still references {stale}")
        target = dest / name
        target.write_text(text, encoding="utf-8")
        target.chmod(0o755 if name.endswith(".sh") else 0o644)
        written.append(name)
    return written


def cmd_check(args: argparse.Namespace) -> int:
    baseline = load_baseline(args.baseline)
    report = run_check(args.src, baseline)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.report:
        args.report.write_text(payload + "\n", encoding="utf-8")
    return 0 if report["status"] == "ok" else 3


def cmd_apply(args: argparse.Namespace) -> int:
    fork = validate_fork(args.fork)
    baseline = load_baseline(args.baseline)
    apply_patch(args.src, baseline, fork)
    print(f"patched {args.src} for {fork}")
    return 0


def cmd_scripts(args: argparse.Namespace) -> int:
    fork = validate_fork(args.fork)
    written = generate_scripts(args.src, args.dest, fork)
    print("generated scripts: " + ", ".join(written))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check upstream against the ad baseline")
    check.add_argument("--src", type=Path, required=True)
    check.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    check.add_argument("--report", type=Path, default=None)
    check.set_defaults(func=cmd_check)

    apply = sub.add_parser("apply", help="apply the ad-removal patch")
    apply.add_argument("--src", type=Path, required=True)
    apply.add_argument("--fork", required=True)
    apply.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    apply.set_defaults(func=cmd_apply)

    scripts = sub.add_parser("scripts", help="generate fork-patched install scripts")
    scripts.add_argument("--src", type=Path, required=True)
    scripts.add_argument("--dest", type=Path, required=True)
    scripts.add_argument("--fork", required=True)
    scripts.set_defaults(func=cmd_scripts)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
