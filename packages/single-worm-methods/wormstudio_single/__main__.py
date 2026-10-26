"""Run with python -m wormstudio_single --help."""
import argparse
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description='Single-worm contour, centerline and temporal contact recovery.')
    sub=parser.add_subparsers(dest='mode',required=True)
    references=sub.add_parser('references',help='Export body16 review suggestions only for missing v4 baseline frames')
    references.add_argument('--baseline',type=Path,required=True)
    references.add_argument('--body16',type=Path,required=True)
    references.add_argument('--output',type=Path,required=True)
    feedback=sub.add_parser('feedback',help='Opt-in one-round SAM2/TAP feedback on a saved single-object event; default is exact passthrough')
    feedback.add_argument('--config',type=Path,required=True)
    feedback.add_argument('--baseline',type=Path,required=True)
    feedback.add_argument('--output',type=Path,required=True)
    feedback.add_argument('--enable-feedback',action='store_true')
    feedback.add_argument('--sam2-checkpoint',type=Path)
    feedback.add_argument('--tapnext-checkpoint',type=Path)
    feedback.add_argument('--radius',type=int,default=8,help='Maximum correction radius in frames,1..30')
    feedback.add_argument('--query-count',type=int,default=32,help='Mask-interior queries,12..64; every third query held out')
    feedback.add_argument('--no-v4-retrack',action='store_true',help='Export mask feedback/new query bank only, skip downstream v4')
    local=sub.add_parser('feedback-local',help='Opt-in short-window feedback v2; default disabled')
    for name in ('config','baseline','output'):local.add_argument('--'+name,type=Path,required=True)
    for name in ('sam2-checkpoint','tapnext-checkpoint'):local.add_argument('--'+name,type=Path)
    local.add_argument('--enable-feedback',action='store_true')
    local.add_argument('--window',type=int,default=17)
    local.add_argument('--stride',type=int,default=8)
    local.add_argument('--max-attempts',type=int,default=3)
    appearance=sub.add_parser('feedback-image',help='Opt-in local feedback with image corroboration; default disabled')
    for name in ('config','baseline','output'):appearance.add_argument('--'+name,type=Path,required=True)
    for name in ('sam2-checkpoint','tapnext-checkpoint'):appearance.add_argument('--'+name,type=Path)
    appearance.add_argument('--enable-feedback',action='store_true')
    for mode in ('replay','run','retrack'):
        p=sub.add_parser(mode,help={'replay':'CPU replay of saved inputs','run':'Fresh SAM2 + TAPNext++ inference',
                                   'retrack':'Cached masks + fresh TAPNext++ evidence'}[mode])
        p.add_argument('--config',type=Path,required=True)
        p.add_argument('--output',type=Path,required=True)
        p.add_argument('--no-video',action='store_true',help='Export arrays and metrics without rendering video')
        p.add_argument('--solver',choices=['auto','v3','v4'],default='auto',help='auto uses v4 with rich tracks, v3 with legacy caches')
        p.add_argument('--cache',type=Path,help='Override the config cache, e.g. replay a retrack output')
        if mode!='replay':
            if mode=='run':p.add_argument('--sam2-checkpoint',type=Path,required=True)
            p.add_argument('--tapnext-checkpoint',type=Path,required=True)
            p.add_argument('--device',choices=['cuda'],default='cuda')
            p.add_argument('--support-count',type=int,default=0,help='Auxiliary queries per primary point, 0..64; default 0')
            p.add_argument('--support-mode',choices=['local','body'],default='local',help='Local grid or mask-constrained support')
            p.add_argument('--support-radius',type=float,default=32.,help='Support square half-width in model-input pixels')
    args=parser.parse_args()
    try:
        if args.mode=='feedback-image':
            if args.enable_feedback:
                from .feedback_image_runner import execute_image
                execute_image(args.config,args.baseline,args.output,args.sam2_checkpoint,args.tapnext_checkpoint)
            else:
                from .feedback_runner import execute_feedback
                execute_feedback(args.config,args.baseline,args.output)
            return
        if args.mode=='feedback-local':
            if not args.enable_feedback:
                from .feedback_runner import execute_feedback
                execute_feedback(args.config,args.baseline,args.output)
            else:
                from .feedback_local import LocalPolicy
                from .feedback_local_runner import execute_local
                execute_local(args.config,args.baseline,args.output,args.sam2_checkpoint,args.tapnext_checkpoint,
                    LocalPolicy(window=args.window,stride=args.stride,max_attempts=args.max_attempts))
            return
        if args.mode=='feedback':
            from .feedback import FeedbackPolicy
            from .feedback_runner import execute_feedback
            execute_feedback(args.config,args.baseline,args.output,args.enable_feedback,args.sam2_checkpoint,args.tapnext_checkpoint,
                FeedbackPolicy(query_count=args.query_count,radius=args.radius),retrack_v4=not args.no_v4_retrack)
            return
        if args.mode=='references':
            import json
            from .references import export_references
            print(json.dumps(export_references(args.baseline,args.body16,args.output),ensure_ascii=False))
            return
        from .pipeline import execute
        execute(args.config,args.output,args.mode,getattr(args,'sam2_checkpoint',None),
                getattr(args,'tapnext_checkpoint',None),getattr(args,'device','cuda'),not args.no_video,args.solver,args.cache,
                getattr(args,'support_count',0),getattr(args,'support_mode','local'),getattr(args,'support_radius',32.))
    except (ValueError,FileNotFoundError) as error:
        parser.error(str(error))


if __name__=='__main__':main()
