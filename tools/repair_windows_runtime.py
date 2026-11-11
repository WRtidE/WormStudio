"""Remove only intentionally retired SAM2 editable files from conda-pack's manifest."""
import argparse
import ast
from pathlib import Path

RETIRED = {
    'Lib/site-packages/__editable___sam_2_1_0_finder.py',
    'Lib/site-packages/sam_2-1.0.dist-info/direct_url.json',
}


def repair(runtime):
    runtime = Path(runtime).resolve()
    script = runtime/'Scripts/conda-unpack-script.py'
    source = script.read_text(encoding='utf-8')
    tree = ast.parse(source)
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == '_prefix_records' for t in node.targets))
    records = ast.literal_eval(assignment.value)
    removed = []
    for row in records:
        name = row[0].replace('\\', '/')
        target = (runtime/name).resolve()
        if not target.is_relative_to(runtime):
            raise ValueError('Unpack path escapes runtime: '+name)
        if not target.is_file():
            if name not in RETIRED:
                raise FileNotFoundError('Unexpected missing runtime file: '+name)
            removed.append(name)
    if removed:
        kept = [row for row in records if row[0].replace('\\', '/') not in removed]
        lines = source.splitlines(keepends=True)
        lines[assignment.lineno-1:assignment.end_lineno] = ['_prefix_records = '+repr(kept)+'\n']
        updated = ''.join(lines)
        ast.parse(updated)
        script.write_text(updated, encoding='utf-8')
    return removed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    print(repair(parser.parse_args().runtime))
