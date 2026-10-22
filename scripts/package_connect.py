"""Refresh both public release snapshots without including credentials or runtime state."""
from pathlib import Path
import hashlib
import shutil
import tarfile

ROOT=Path(__file__).resolve().parents[1]


def archive(output, base, prefix, files):
    with tarfile.open(output,'w:gz') as tar:
        for path in files:
            info=tar.gettarinfo(str(path),arcname=prefix+'/'+str(path.relative_to(base)))
            info.uid=info.gid=0;info.uname=info.gname='';info.mode=0o644
            with path.open('rb') as stream:tar.addfile(info,stream)


def main():
    general=ROOT/'agent_connect';buddy=ROOT/'agent_connect_workbuddy'
    shutil.copy2(ROOT/'packages/community-skill/SKILL.md',general/'skills/agora-community/SKILL.md')
    files=[ROOT/'pyproject.toml']+sorted(p for p in (ROOT/'src/agent_agora').rglob('*')
        if p.is_file() and p.suffix in {'.py','.html','.js','.css'} and '__pycache__' not in p.parts)
    archive(general/'sdk/agent-agora-lite-source.tar.gz',ROOT,'agent-agora-lite',files)
    shutil.copy2(general/'sdk/agent-agora-lite-source.tar.gz',buddy/'sdk/agent-agora-lite-source.tar.gz')
    adapter=ROOT/'adapters/workbuddy'
    archive(buddy/'sdk/agent-agora-workbuddy-source.tar.gz',adapter,'agent-agora-workbuddy',
            [adapter/'pyproject.toml']+sorted((adapter/'src').rglob('*.py')))
    for root in (general,buddy):
        (root/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(root))+'\n'
            for p in sorted(root.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'
            and not {'private','.venv','__pycache__'}.intersection(p.relative_to(root).parts)))
    print('Refreshed general core and independent WorkBuddy releases; private configuration excluded.')

if __name__=='__main__':main()
