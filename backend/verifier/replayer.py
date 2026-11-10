from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Browser

from core.event_bus import EventBus
from detector.objective import evaluate_objective_signals, probe_page_health
from detector.signatures import normalize_url_path
from detector.visual_judge import VisualJudge
from storage.artifacts import ArtifactStore
from storage.db import Database, utc_now_iso
from workers.browser_session import BrowserSession


def _parse_viewport(viewport_str: str) -> tuple[int, int, bool]:
    try:
        w_s, h_s = viewport_str.lower().split("x")
        w, h = int(w_s), int(h_s)
        return w, h, w <= 500
    except Exception:
        return 1280, 800, False


def _signature_matches(expected_sig: str, candidate_sig: str, expected_cat: str, candidate_cat: str) -> bool:
    if expected_sig == candidate_sig:
        return True
    if expected_cat != candidate_cat:
        return False
    exp_parts = expected_sig.split(":", 2)
    cand_parts = candidate_sig.split(":", 2)
    if len(exp_parts) == 3 and len(cand_parts) == 3:
        if exp_parts[0] != cand_parts[0] or exp_parts[1] != cand_parts[1]:
            return False
        t1 = set(exp_parts[2].split())
        t2 = set(cand_parts[2].split())
        if t1 and t2 and (len(t1 & t2) / len(t1 | t2)) >= 0.6:
            return True
    return False


class ReplayVerifier:
    """Replays suspected bug traces in fresh browser contexts with a 2-of-3 reproduction gate."""

    def __init__(
        self,
        *,
        browser: Browser,
        db: Database,
        artifacts: ArtifactStore,
        visual_judge: VisualJudge,
        event_bus: EventBus,
    ) -> None:
        self.browser = browser
        self.db = db
        self.artifacts = artifacts
        self.visual_judge = visual_judge
        self.event_bus = event_bus

    async def verify_suspect(self, suspect_id: str) -> tuple[bool, str | None]:
        suspect = await self.db.get_suspect(suspect_id)
        if not suspect:
            return False, None
        run_id = suspect["run_id"]
        run_row = await self.db.get_run(run_id)
        if not run_row:
            return False, None

        worker_row = await self.db.get_worker(suspect["worker_id"])
        vw, vh, has_touch = _parse_viewport((worker_row or {}).get("viewport", "1280x800"))

        await self.db.update_suspect_status(suspect_id, "Verifying")
        await self.event_bus.publish(
            run_id,
            "suspect_verifying",
            {
                "suspect_id": suspect_id,
                "status": "Verifying",
                "title_hint": suspect["title_hint"],
                "category": suspect["category"],
                "persona": suspect["persona"],
            },
        )

        trace_slice: list[dict[str, Any]] = json.loads(suspect["trace_slice_json"] or "[]")
        evidence: dict[str, Any] = json.loads(suspect["evidence_json"] or "{}")
        target_url = run_row["target_url"]
        expected_sig = suspect["error_signature"]
        expected_cat = suspect["category"]
        videos_dir = self.artifacts.videos_dir(run_id)

        passes = 0
        fails = 0
        winning_video_rel: str | None = None
        last_notes: list[str] = []

        for attempt in range(1, 4):
            t0 = time.perf_counter()
            tmp_vid_dir = videos_dir / f"tmp_{suspect_id}_{attempt}"
            tmp_vid_dir.mkdir(parents=True, exist_ok=True)
            session = BrowserSession(
                self.browser,
                viewport_width=vw,
                viewport_height=vh,
                has_touch=has_touch,
                record_video_dir=str(tmp_vid_dir),
            )
            matched_sig: str | None = None
            reproduced = False
            note = "Did not reproduce"
            shot_rel: str | None = None

            try:
                page = await session.start()
                await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                await session.settle()

                # Check if defect occurred immediately upon landing
                init_health = await probe_page_health(page)
                init_events = session.drain_events()
                init_xss = init_events.xss_triggered or await session.check_xss_sentinel()
                init_signals = evaluate_objective_signals(
                    before=init_health,
                    after=init_health,
                    events=init_events,
                    xss_hit=init_xss,
                    action_type="navigate",
                    locator_json={},
                    action_error=None,
                )
                for sig_item in init_signals:
                    if _signature_matches(expected_sig, sig_item.error_signature, expected_cat, sig_item.category):
                        reproduced = True
                        matched_sig = sig_item.error_signature
                        note = f"Reproduced on initial load ({matched_sig})"
                        break

                prev_health = init_health
                for step in trace_slice:
                    if reproduced:
                        break
                    action = step.get("action_type", "wait")
                    locator = step.get("locator") or {}
                    val = step.get("input_value")
                    action_err: str | None = None
                    try:
                        await session.execute_action(action, locator, val)
                    except Exception as exc:
                        action_err = str(exc)

                    after_health = await probe_page_health(page)
                    events = session.drain_events()
                    xss_hit = events.xss_triggered or await session.check_xss_sentinel()
                    signals = evaluate_objective_signals(
                        before=prev_health,
                        after=after_health,
                        events=events,
                        xss_hit=xss_hit,
                        action_type=action,
                        locator_json=locator,
                        action_error=action_err,
                    )
                    for sig_item in signals:
                        if _signature_matches(expected_sig, sig_item.error_signature, expected_cat, sig_item.category):
                            reproduced = True
                            matched_sig = sig_item.error_signature
                            note = f"Reproduced on step {step.get('step_index')}: {matched_sig}"
                            break
                    prev_health = after_health

                # Fallback visual check if suspect came purely from visual LLM judge
                if not reproduced and expected_sig.startswith("visual:"):
                    replay_jpg = await page.screenshot(type="jpeg", quality=75, full_page=False)
                    orig_path = evidence.get("screenshot_path")
                    orig_bytes = (
                        self.artifacts.resolve(orig_path).read_bytes()
                        if orig_path and self.artifacts.resolve(orig_path).exists()
                        else None
                    )
                    vis_ok, vis_reason = await self.visual_judge.verify_replay_screenshot(
                        run_id=run_id,
                        symptom=suspect["title_hint"],
                        original_jpeg=orig_bytes,
                        replay_jpeg=replay_jpg,
                    )
                    if vis_ok:
                        reproduced = True
                        matched_sig = expected_sig
                        note = f"Visual judge confirmed: {vis_reason}"
                    else:
                        note = f"Visual judge rejected: {vis_reason}"

                shot_path = videos_dir / f"{suspect_id}_att{attempt}.jpg"
                shot_bytes = await page.screenshot(type="jpeg", quality=75, full_page=False)
                shot_path.write_bytes(shot_bytes)
                shot_rel = self.artifacts.to_relative(shot_path)
            except Exception as exc:
                note = f"Replay error: {str(exc)[:140]}"
            finally:
                raw_vid = await session.close()

            final_vid_rel: str | None = None
            if raw_vid and Path(raw_vid).exists():
                if reproduced and winning_video_rel is None:
                    dest = videos_dir / f"{suspect_id}.webm"
                    shutil.move(raw_vid, dest)
                    final_vid_rel = self.artifacts.to_relative(dest)
                    winning_video_rel = final_vid_rel
                shutil.rmtree(tmp_vid_dir, ignore_errors=True)
            else:
                shutil.rmtree(tmp_vid_dir, ignore_errors=True)

            duration_ms = int((time.perf_counter() - t0) * 1000)
            await self.db.insert_verification_attempt(
                suspect_id=suspect_id,
                attempt_num=attempt,
                reproduced=reproduced,
                matched_signature=matched_sig,
                video_path=final_vid_rel,
                screenshot_path=shot_rel,
                notes=note,
                duration_ms=duration_ms,
            )
            last_notes.append(f"#{attempt}: {note}")
            if reproduced:
                passes += 1
            else:
                fails += 1

            # 2-of-3 early decision
            if passes >= 2 or fails >= 2:
                break

        verified = passes >= 2
        if not verified:
            if winning_video_rel:
                try:
                    self.artifacts.resolve(winning_video_rel).unlink(missing_ok=True)
                except Exception:
                    pass
                winning_video_rel = None
            reason = f"Reproduced {passes}/3 times ({'; '.join(last_notes)})"
            await self.db.update_suspect_status(
                suspect_id, "Rejected", repro_count=passes, rejection_reason=reason
            )
            await self.event_bus.publish(
                run_id,
                "suspect_rejected",
                {
                    "suspect_id": suspect_id,
                    "status": "Rejected",
                    "repro_count": passes,
                    "rejection_reason": reason,
                    "title_hint": suspect["title_hint"],
                    "category": suspect["category"],
                    "persona": suspect["persona"],
                    "url": normalize_url_path(suspect["url"]),
                },
            )
        else:
            await self.db.update_suspect_status(suspect_id, "Verified", repro_count=passes)
            await self.db.update_run_status(run_id, "running", first_verified_at=utc_now_iso())
            await self.event_bus.publish(
                run_id,
                "suspect_verified",
                {
                    "suspect_id": suspect_id,
                    "status": "Verified",
                    "repro_count": passes,
                    "title_hint": suspect["title_hint"],
                    "category": suspect["category"],
                    "persona": suspect["persona"],
                    "url": suspect["url"],
                    "video_path": winning_video_rel,
                },
            )
        return verified, winning_video_rel

