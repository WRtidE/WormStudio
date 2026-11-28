"""File-first local workspace; reuse the existing annotation and analysis pipeline."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from resource_paths import ensure_runtime


def main(open_browser=True):
    if open_browser:
        from electron_desktop import launch
        return launch()
    from manual_worm_annotation import create_server
    from pipeline_progress import PipelineProgress
    from worm_pipeline import _main, video_config

    progress = PipelineProgress()
    from batch_queue import BatchQueue
    progress.batch = BatchQueue(Path(__file__).resolve().parent / 'config/batch.local.json')
    progress.data.update(status='ready', detail='选择一个视频，开始新的分析')
    server = create_server(progress=progress)
    server.studio_defaults = video_config()
    progress.bind_server(server)
    url = progress.url.removesuffix('/progress') + '/studio'
    print(f'Worm Studio: {url}', flush=True)
    try:
        while (settings := progress.wait()) is not None:
            try:
                followup = _main(progress, settings)
                # Check-only and dry runs can return before publishing a result.
                with progress.lock:
                    if progress.batch.active is not None:
                        progress.batch.finish(progress.data['status'], progress.data.get('error'),
                                              cancelled=progress.cancel_requested.is_set())
            except KeyboardInterrupt:
                progress.fail('用户在终端中断了处理', cancelled=True, finish_batch=True)
                break
            except Exception as exc:
                progress.fail(exc, cancelled=progress.cancel_requested.is_set(), finish_batch=True)
                print(f'处理停止：{exc}', file=sys.stderr, flush=True)
                continue
            if followup:
                group, sample, members = followup
                from chamber_videos import members_for_video
                try:
                    ready, missing = members_for_video(members, sample, partial=True)
                    if not missing:
                        server.chamber_videos.start(group, ready, full=True)
                except (OSError, ValueError) as exc:
                    print(f'全槽视频暂未生成，可在结果页重试：{exc}', flush=True)
    finally:
        progress.close()


if __name__ == '__main__':
    ensure_runtime()
    raise SystemExit(main())
