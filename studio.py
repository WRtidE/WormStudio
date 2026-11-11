"""File-first local workspace; reuse the existing annotation and analysis pipeline."""
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from resource_paths import ensure_runtime


def main(open_browser=True):
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
    if open_browser:
        webbrowser.open(url, new=2)
    try:
        while (settings := progress.wait()) is not None:
            try:
                _main(progress, settings)
            except KeyboardInterrupt:
                progress.fail('用户在终端中断了处理', cancelled=True)
                break
            except Exception as exc:
                progress.fail(exc, cancelled=progress.cancel_requested.is_set())
                print(f'处理停止：{exc}', file=sys.stderr, flush=True)
            finally:
                with progress.lock:
                    progress.batch.finish(progress.data['status'], progress.data.get('error'),
                                          cancelled=progress.cancel_requested.is_set(),
                                          result_sample=progress.data.get('sample'))
    finally:
        progress.close()


if __name__ == '__main__':
    ensure_runtime()
    main()
