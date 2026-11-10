from __future__ import annotations

from pathlib import Path


class ArtifactStore:
    """Manages filesystem paths for screenshots, verification videos, and exports."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        path = self.data_dir / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def worker_steps_dir(self, run_id: str, worker_id: str) -> Path:
        path = self.run_dir(run_id) / "workers" / worker_id / "steps"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def videos_dir(self, run_id: str) -> Path:
        path = self.run_dir(run_id) / "videos"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def exports_dir(self, run_id: str) -> Path:
        path = self.run_dir(run_id) / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def step_screenshot_paths(
        self, run_id: str, worker_id: str, step_index: int
    ) -> tuple[Path, Path]:
        steps_dir = self.worker_steps_dir(run_id, worker_id)
        clean = steps_dir / f"step_{step_index:03d}.jpg"
        som = steps_dir / f"step_{step_index:03d}_som.jpg"
        return clean, som

    def to_relative(self, path: Path) -> str:
        """Return a forward-slash path relative to data_dir for DB portability."""
        try:
            return path.resolve().relative_to(self.data_dir.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def resolve(self, relative_or_abs: str) -> Path:
        p = Path(relative_or_abs)
        return p if p.is_absolute() else (self.data_dir / p)

