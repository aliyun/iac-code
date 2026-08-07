#!/usr/bin/env python3
"""Generate reproducible Desktop SBOM, notices, and a release privacy notice."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
PRIVACY_TEMPLATE = DESKTOP / "release/PRIVACY_NOTICE.template.md"
LICENSE_PREFIXES = ("license", "copying", "notice", "copyright")
PRIVACY_FIELDS = ("LEGAL_ENTITY", "PRIVACY_CONTACT", "TELEMETRY_RETENTION", "EFFECTIVE_DATE")


@dataclass
class Component:
    ecosystem: str
    name: str
    version: str
    license_expression: str
    role: str
    license_texts: dict[str, str] = field(default_factory=dict)

    @property
    def purl(self) -> str:
        purl_name = canonicalize_name(self.name) if self.ecosystem == "pypi" else self.name
        encoded_name = quote(purl_name, safe="/" if self.ecosystem == "npm" else "")
        return "pkg:{}/{}@{}".format(self.ecosystem, encoded_name, quote(self.version, safe=".+-"))


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").rstrip() + "\n"


def _is_license_file(path: Path) -> bool:
    return path.name.lower().startswith(LICENSE_PREFIXES)


def _declared_python_license(dist: metadata.Distribution, texts: dict[str, str]) -> str:
    declared = (dist.metadata.get("License-Expression") or dist.metadata.get("License") or "").strip()
    if declared and declared.upper() not in {"UNKNOWN", "NONE"} and "\n" not in declared and len(declared) <= 200:
        return declared
    classifiers = [
        value.split("License ::", 1)[1].strip()
        for value in dist.metadata.get_all("Classifier", [])
        if "License ::" in value
    ]
    if classifiers:
        return "; ".join(sorted(set(classifiers)))
    if texts:
        return "LicenseRef-See-Notice"
    raise RuntimeError("Python dependency {} has no license declaration or license text".format(dist.metadata["Name"]))


def _python_license_texts(dist: metadata.Distribution) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in dist.files or []:
        relative = Path(str(entry))
        if not _is_license_file(relative):
            continue
        text = _read_text(Path(dist.locate_file(entry)))
        if text:
            result[str(relative).replace("\\", "/")] = text
    return result


def python_components(root: Path = ROOT) -> list[Component]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    seeds = list(project["project"]["dependencies"])
    seeds.extend(
        requirement
        for requirement in project["dependency-groups"]["desktop"]
        if canonicalize_name(Requirement(requirement).name) != "pyinstaller"
    )
    distributions = {
        canonicalize_name(dist.metadata["Name"]): dist
        for dist in metadata.distributions()
        if dist.metadata.get("Name")
    }
    active_extras: dict[str, set[str]] = {}
    queue: list[str] = []
    for raw in seeds:
        requirement = Requirement(raw)
        name = canonicalize_name(requirement.name)
        active_extras.setdefault(name, set()).update(requirement.extras)
        queue.append(name)

    processed_extras: dict[str, frozenset[str]] = {}
    components: dict[str, Component] = {}
    environment = default_environment()
    while queue:
        name = queue.pop()
        extras = frozenset(active_extras.get(name, set()))
        if processed_extras.get(name) == extras:
            continue
        processed_extras[name] = extras
        dist = distributions.get(name)
        if dist is None:
            raise RuntimeError("Desktop Python dependency is not installed: {}".format(name))
        texts = _python_license_texts(dist)
        component = Component(
            ecosystem="pypi",
            name=dist.metadata["Name"],
            version=dist.version,
            license_expression=_declared_python_license(dist, texts),
            role="runtime",
            license_texts=texts,
        )
        components[component.purl] = component
        marker_extras = extras or frozenset({""})
        for raw_requirement in dist.requires or []:
            requirement = Requirement(raw_requirement)
            if requirement.marker and not any(
                requirement.marker.evaluate({**environment, "extra": extra}) for extra in marker_extras
            ):
                continue
            dependency_name = canonicalize_name(requirement.name)
            before = frozenset(active_extras.get(dependency_name, set()))
            active_extras.setdefault(dependency_name, set()).update(requirement.extras)
            if dependency_name not in processed_extras or before != frozenset(active_extras[dependency_name]):
                queue.append(dependency_name)
    return sorted(components.values(), key=lambda component: component.purl)


def _cargo_metadata(manifest: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "cargo",
            "metadata",
            "--locked",
            "--format-version",
            "1",
            "--manifest-path",
            str(manifest),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _cargo_license_texts(package: dict[str, Any]) -> dict[str, str]:
    manifest_dir = Path(package["manifest_path"]).parent
    candidates: list[Path] = []
    if package.get("license_file"):
        license_file = Path(package["license_file"])
        candidates.append(license_file if license_file.is_absolute() else manifest_dir / license_file)
    candidates.extend(path for path in manifest_dir.iterdir() if path.is_file() and _is_license_file(path))
    result: dict[str, str] = {}
    for path in sorted(set(candidates), key=lambda value: value.name.lower()):
        text = _read_text(path)
        if text:
            result[path.name] = text
    return result


def cargo_components(root: Path = ROOT) -> list[Component]:
    components: dict[str, Component] = {}
    for relative in ("desktop/src-tauri/Cargo.toml", "desktop/helpers/Cargo.toml"):
        cargo = _cargo_metadata(root / relative)
        for package in cargo["packages"]:
            if not package.get("source"):
                continue
            texts = _cargo_license_texts(package)
            declared = (package.get("license") or "").strip()
            if not declared and not texts:
                raise RuntimeError("Cargo dependency {} has no license evidence".format(package["name"]))
            component = Component(
                ecosystem="cargo",
                name=package["name"],
                version=package["version"],
                license_expression=declared or "LicenseRef-See-Notice",
                role="runtime-and-build",
                license_texts=texts,
            )
            existing = components.get(component.purl)
            if existing:
                existing.license_texts.update(component.license_texts)
            else:
                components[component.purl] = component
    return sorted(components.values(), key=lambda component: component.purl)


def _npm_name(relative: str, info: dict[str, Any]) -> str:
    if info.get("name"):
        return str(info["name"])
    marker = "node_modules/"
    return relative.rsplit(marker, 1)[-1]


def npm_components(root: Path = ROOT) -> list[Component]:
    lock = json.loads((root / "desktop/package-lock.json").read_text(encoding="utf-8"))
    components: dict[str, Component] = {}
    for relative, info in lock["packages"].items():
        if not relative or not info.get("version"):
            continue
        installed = root / "desktop" / relative
        if not installed.is_dir():
            continue
        texts: dict[str, str] = {}
        for path in installed.iterdir():
            if path.is_file() and _is_license_file(path):
                text = _read_text(path)
                if text:
                    texts[path.name] = text
        declared = str(info.get("license") or "").strip()
        if not declared and not texts:
            raise RuntimeError("npm dependency {} has no license evidence".format(_npm_name(relative, info)))
        component = Component(
            ecosystem="npm",
            name=_npm_name(relative, info),
            version=str(info["version"]),
            license_expression=declared or "LicenseRef-See-Notice",
            role="build",
            license_texts=texts,
        )
        components[component.purl] = component
    return sorted(components.values(), key=lambda component: component.purl)


def collect_components(root: Path = ROOT) -> list[Component]:
    components: dict[str, Component] = {}
    for component in [*python_components(root), *cargo_components(root), *npm_components(root)]:
        existing = components.get(component.purl)
        if existing:
            existing.license_texts.update(component.license_texts)
        else:
            components[component.purl] = component
    return sorted(components.values(), key=lambda component: component.purl)


def desktop_version(root: Path = ROOT) -> str:
    package = json.loads((root / "desktop/package.json").read_text(encoding="utf-8"))
    return str(package["version"])


def build_sbom(components: Iterable[Component], version: str) -> dict[str, Any]:
    serialized = []
    for component in sorted(components, key=lambda value: value.purl):
        serialized.append(
            {
                "type": "library",
                "bom-ref": component.purl,
                "name": component.name,
                "version": component.version,
                "purl": component.purl,
                # Dependency metadata is not guaranteed to use a valid SPDX
                # expression (Python classifiers often use descriptive names).
                "licenses": [{"license": {"name": component.license_expression}}],
                "properties": [
                    {"name": "iac-code:ecosystem", "value": component.ecosystem},
                    {"name": "iac-code:dependency-role", "value": component.role},
                ],
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "iac-code Desktop",
                "version": version,
                "bom-ref": "pkg:generic/iac-code-desktop@{}".format(version),
            },
            "properties": [
                {"name": "iac-code:reproducible", "value": "true"},
                {"name": "iac-code:inventory-scope", "value": "desktop-runtime-and-build"},
            ],
        },
        "components": serialized,
    }


def render_notices(components: Iterable[Component]) -> str:
    lines = [
        "iac-code Desktop Third-Party Notices",
        "====================================",
        "",
        "Generated from the dependencies used by the Desktop runtime and build. This file does not",
        "alter the license terms of any dependency. Build-only tools are labelled explicitly.",
        "",
    ]
    for component in sorted(components, key=lambda value: value.purl):
        lines.extend(
            [
                "{} {} ({})".format(component.name, component.version, component.ecosystem),
                "-" * min(100, len(component.name) + len(component.version) + len(component.ecosystem) + 4),
                "PURL: {}".format(component.purl),
                "Declared license: {}".format(component.license_expression),
                "Dependency role: {}".format(component.role),
            ]
        )
        if component.license_texts:
            for name, text in sorted(component.license_texts.items()):
                lines.extend(["", "--- {} ---".format(name), text.rstrip()])
        else:
            lines.append("License text: see the declared upstream license expression.")
        lines.extend(["", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_privacy_notice(template: str, values: dict[str, str]) -> str:
    missing = [field for field in PRIVACY_FIELDS if not values.get(field, "").strip()]
    if missing:
        raise RuntimeError("privacy notice fields are missing: {}".format(", ".join(missing)))
    rendered = template
    for placeholder in PRIVACY_FIELDS:
        rendered = rendered.replace("{{" + placeholder + "}}", values[placeholder].strip())
    unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", rendered)))
    if unresolved:
        raise RuntimeError("privacy notice contains unresolved fields: {}".format(", ".join(unresolved)))
    return rendered.rstrip() + "\n"


def generate(output_dir: Path, *, root: Path = ROOT) -> tuple[Path, Path]:
    components = collect_components(root)
    if not components:
        raise RuntimeError("Desktop release inventory is empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    sbom_path = output_dir / "desktop-sbom.cdx.json"
    notices_path = output_dir / "THIRD_PARTY_NOTICES.txt"
    sbom_path.write_text(
        json.dumps(build_sbom(components, desktop_version(root)), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    notices_path.write_text(render_notices(components), encoding="utf-8")
    return sbom_path, notices_path


def verify_reproducible(output_dir: Path, *, root: Path = ROOT) -> None:
    with tempfile.TemporaryDirectory(prefix="iac-code-desktop-release-") as temporary:
        generated = Path(temporary)
        generate(generated, root=root)
        for name in ("desktop-sbom.cdx.json", "THIRD_PARTY_NOTICES.txt"):
            expected = (output_dir / name).read_bytes()
            actual = (generated / name).read_bytes()
            if expected != actual:
                raise RuntimeError("Desktop release metadata is not reproducible: {}".format(name))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--render-privacy", action="store_true")
    parser.add_argument("--legal-entity")
    parser.add_argument("--privacy-contact")
    parser.add_argument("--telemetry-retention")
    parser.add_argument("--effective-date")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.verify:
        verify_reproducible(args.output_dir)
    else:
        generate(args.output_dir)
    if args.render_privacy:
        values = {
            "LEGAL_ENTITY": args.legal_entity or "",
            "PRIVACY_CONTACT": args.privacy_contact or "",
            "TELEMETRY_RETENTION": args.telemetry_retention or "",
            "EFFECTIVE_DATE": args.effective_date or "",
        }
        notice = render_privacy_notice(PRIVACY_TEMPLATE.read_text(encoding="utf-8"), values)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "PRIVACY_NOTICE.md").write_text(notice, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
