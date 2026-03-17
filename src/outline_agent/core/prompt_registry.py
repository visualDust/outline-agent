from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppSettings


@dataclass(frozen=True)
class PromptPack:
    name: str
    text: str


@dataclass(frozen=True)
class PromptRegistry:
    system_prompt_path: Path
    prompt_pack_dir: Path
    internal_prompt_dir: Path
    user_prompt_roots: tuple[Path, ...]
    prompt_pack_roots: tuple[Path, ...]
    internal_prompt_roots: tuple[Path, ...]

    @classmethod
    def from_settings(cls, settings: AppSettings) -> PromptRegistry:
        from .config import (
            PROJECT_ROOT,
            get_config_root,
            get_package_internal_prompt_dir,
            get_package_user_prompt_root,
        )

        def _dedupe(paths: list[Path]) -> tuple[Path, ...]:
            seen: set[Path] = set()
            ordered: list[Path] = []
            for path in paths:
                resolved = path
                if resolved in seen:
                    continue
                seen.add(resolved)
                ordered.append(resolved)
            return tuple(ordered)

        config_prompt_root = get_config_root() / "prompts"
        package_user_root = get_package_user_prompt_root()
        package_internal_root = get_package_internal_prompt_dir()
        project_prompt_root = PROJECT_ROOT / "prompts"
        standard_user_roots = [
            config_prompt_root / "user",
            project_prompt_root / "user",
            package_user_root,
        ]
        standard_pack_roots = [
            config_prompt_root / "user/packs",
            project_prompt_root / "user/packs",
            package_user_root / "packs",
        ]
        standard_internal_roots = [
            config_prompt_root / "internal",
            project_prompt_root / "internal",
            package_internal_root,
        ]
        custom_user_root = settings.system_prompt_path.parent
        custom_pack_root = settings.prompt_pack_dir
        custom_internal_root = settings.internal_prompt_dir

        user_root_candidates = (
            [custom_user_root, *standard_user_roots]
            if custom_user_root not in standard_user_roots
            else standard_user_roots
        )
        pack_root_candidates = (
            [custom_pack_root, *standard_pack_roots]
            if custom_pack_root not in standard_pack_roots
            else standard_pack_roots
        )
        internal_root_candidates = (
            [custom_internal_root, *standard_internal_roots]
            if custom_internal_root not in standard_internal_roots
            else standard_internal_roots
        )

        return cls(
            system_prompt_path=settings.system_prompt_path,
            prompt_pack_dir=settings.prompt_pack_dir,
            internal_prompt_dir=settings.internal_prompt_dir,
            user_prompt_roots=_dedupe(user_root_candidates),
            prompt_pack_roots=_dedupe(pack_root_candidates),
            internal_prompt_roots=_dedupe(internal_root_candidates),
        )

    @property
    def user_prompt_dir(self) -> Path:
        return self.system_prompt_path.parent

    def load_system_prompt(self) -> str:
        return _load_required_text_file(self.system_prompt_path)

    def load_prompt_packs(self, pack_names: list[str]) -> list[PromptPack]:
        packs: list[PromptPack] = []
        for name in pack_names:
            if not name:
                continue
            text = _load_first_optional_text_file(
                [root / f"{name}.md" for root in self.prompt_pack_roots]
            )
            if not text:
                continue
            packs.append(PromptPack(name=name, text=text))
        return packs

    def load_user_optional(self, relative_path: str) -> str | None:
        return _load_first_optional_text_file([root / relative_path for root in self.user_prompt_roots])

    def load_internal_required(self, relative_path: str) -> str:
        text = _load_first_optional_text_file([root / relative_path for root in self.internal_prompt_roots])
        if text is None:
            raise ValueError(f"Required internal prompt does not exist or is empty: {relative_path}")
        return text

    def load_internal_optional(self, relative_path: str) -> str | None:
        return _load_first_optional_text_file([root / relative_path for root in self.internal_prompt_roots])

    def compose_internal_prompt(self, protocol_text: str, relative_path: str) -> str:
        policy_text = self.load_internal_required(relative_path)
        protocol = protocol_text.strip()
        policy = policy_text.strip()
        if not policy:
            return protocol
        return f"{protocol}\n\nAdditional internal policy:\n{policy}"


@lru_cache(maxsize=128)
def _load_optional_text_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return text or None


def _load_first_optional_text_file(paths: list[Path]) -> str | None:
    for path in paths:
        text = _load_optional_text_file(path)
        if text is not None:
            return text
    return None


def _load_required_text_file(path: Path) -> str:
    text = _load_optional_text_file(path)
    if text is None:
        raise ValueError(f"Required text file does not exist or is empty: {path}")
    return text
