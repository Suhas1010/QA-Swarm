from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from storage.artifacts import ArtifactStore


class GitHubIssueClient:
    """Files verified bugs to GitHub REST API (with branch media upload) or exports Markdown fallback."""

    def __init__(self, *, token: str = "", repo: str = "", artifacts: ArtifactStore) -> None:
        self.token = token.strip()
        self.repo = repo.strip()
        self.artifacts = artifacts

    @property
    def can_post_github(self) -> bool:
        return bool(self.token and self.repo and "/" in self.repo)

    async def _ensure_evidence_branch(self, client: httpx.AsyncClient, branch: str = "qa-swarm-evidence") -> bool:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        check = await client.get(
            f"https://api.github.com/repos/{self.repo}/git/ref/heads/{branch}",
            headers=headers,
        )
        if check.status_code == 200:
            return True

        repo_info = await client.get(f"https://api.github.com/repos/{self.repo}", headers=headers)
        if repo_info.status_code != 200:
            return False
        default_branch = repo_info.json().get("default_branch", "main")
        base_ref = await client.get(
            f"https://api.github.com/repos/{self.repo}/git/ref/heads/{default_branch}",
            headers=headers,
        )
        if base_ref.status_code != 200:
            return False
        sha = base_ref.json()["object"]["sha"]
        create = await client.post(
            f"https://api.github.com/repos/{self.repo}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        return create.status_code in {200, 201}

    async def _upload_evidence_file(
        self,
        client: httpx.AsyncClient,
        local_path: Path,
        repo_path: str,
        branch: str = "qa-swarm-evidence",
    ) -> str | None:
        if not local_path.exists() or local_path.stat().st_size > 24 * 1024 * 1024:
            return None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        encoded = base64.b64encode(local_path.read_bytes()).decode("ascii")
        res = await client.put(
            f"https://api.github.com/repos/{self.repo}/contents/{repo_path}",
            headers=headers,
            json={
                "message": f"Add QA Swarm evidence {repo_path}",
                "content": encoded,
                "branch": branch,
            },
        )
        if res.status_code in {200, 201}:
            return f"https://github.com/{self.repo}/blob/{branch}/{repo_path}?raw=true"
        return None

    async def publish_bug(
        self,
        *,
        run_id: str,
        bug_key: str,
        title: str,
        severity: str,
        category: str,
        body_markdown: str,
        video_rel_path: str | None = None,
        screenshot_rel_path: str | None = None,
    ) -> dict[str, Any]:
        """Export Markdown locally and optionally create a GitHub Issue with evidence links."""
        exports_dir = self.artifacts.exports_dir(run_id)
        md_path = exports_dir / f"{bug_key}.md"
        md_path.write_text(body_markdown, encoding="utf-8")
        md_rel = self.artifacts.to_relative(md_path)

        if not self.can_post_github:
            return {
                "github_issue_url": None,
                "github_issue_number": None,
                "markdown_export_path": md_rel,
            }

        issue_body = body_markdown
        async with httpx.AsyncClient(timeout=20.0) as client:
            if await self._ensure_evidence_branch(client):
                media_lines: list[str] = []
                if screenshot_rel_path:
                    shot_url = await self._upload_evidence_file(
                        client,
                        self.artifacts.resolve(screenshot_rel_path),
                        f"evidence/{run_id}/{bug_key}.jpg",
                    )
                    if shot_url:
                        media_lines.append(f"![Evidence Screenshot]({shot_url})")
                if video_rel_path:
                    vid_url = await self._upload_evidence_file(
                        client,
                        self.artifacts.resolve(video_rel_path),
                        f"evidence/{run_id}/{bug_key}.webm",
                    )
                    if vid_url:
                        media_lines.append(f"[Download/View Verification Recording (.webm)]({vid_url})")
                if media_lines:
                    issue_body = issue_body + "\n\n## Hosted Evidence\n" + "\n\n".join(media_lines)

            headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
            }
            resp = await client.post(
                f"https://api.github.com/repos/{self.repo}/issues",
                headers=headers,
                json={
                    "title": title,
                    "body": issue_body,
                    "labels": ["qa-swarm", severity.lower(), category],
                },
            )
            if resp.status_code in {200, 201}:
                data = resp.json()
                return {
                    "github_issue_url": data.get("html_url"),
                    "github_issue_number": data.get("number"),
                    "markdown_export_path": md_rel,
                }

        return {
            "github_issue_url": None,
            "github_issue_number": None,
            "markdown_export_path": md_rel,
        }

