"""Bounded immutable-checkout source capture; no caller command or credentials."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

MAX_FILES = 8
MAX_CONTENT_BYTES = 65536
METHOD = 'repository-source-capture.v1'


class SourceCaptureFile(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    path: str = Field(min_length=1, max_length=512)
    blob_sha: str = Field(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def safe_path(self):
        p = PurePosixPath(self.path)
        if (p.is_absolute() or str(p) != self.path or any(x in {'.', '..', '.git'} for x in p.parts)
                or '\\' in self.path or any(ord(x) < 32 or ord(x) == 127 for x in self.path)):
            raise ValueError('Source capture requires a canonical relative repository file path')
        return self


class SourceCaptureRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    method: Literal['repository-source-capture.v1'] = METHOD
    request_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    expected_fence: StrictInt = Field(ge=1)
    work_item_ref: str = Field(min_length=1, max_length=512)
    repository_resource_id: str = Field(pattern=r'^[A-Za-z0-9._:-]{1,200}$')
    commit_sha: str = Field(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
    files: tuple[SourceCaptureFile, ...] = Field(min_length=1, max_length=MAX_FILES)

    @model_validator(mode='after')
    def unique_files(self):
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError('Source capture paths must be unique')
        return self


# Code-owned program, executed only by the existing isolated worker backend.
# Git plumbing reads immutable objects without checkout filters/hooks; openat
# with NOFOLLOW verifies the actual admitted files, never a host-side upload.
CAPTURE_PROGRAM = r'''
import base64,hashlib,json,os,stat,subprocess,sys
r=json.loads(sys.argv[1]); limit=65536; total=0
def git(*args):
 p=subprocess.run(['/usr/bin/git','--literal-pathspecs','--no-optional-locks','-c','core.fsmonitor=false',
  '-c','core.hooksPath=/dev/null','-c','protocol.allow=never',*args],check=True,capture_output=True,timeout=3)
 return p.stdout
assert git('rev-parse','--verify','HEAD').decode().strip()==r['commit_sha'],'source HEAD changed'
root=os.open('.',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
files=[]
try:
 for f in r['files']:
  obj=r['commit_sha']+':'+f['path']
  tree=git('ls-tree','-z',r['commit_sha'],'--',f['path']).split(b'\0')
  assert len(tree)==2 and tree[1]==b'','source is not one exact tracked file'
  header,path=tree[0].split(b'\t',1); mode,kind,sha=header.split()
  assert path.decode()==f['path'] and mode in (b'100644',b'100755') and kind==b'blob','source is not regular'
  assert sha.decode()==f['blob_sha'] and git('cat-file','-t',obj)==b'blob\n','source blob changed'
  size=int(git('cat-file','-s',obj)); total+=size
  assert 0<=size<=limit and total<=limit,'source capture byte limit exceeded'
  body=git('cat-file','blob',obj)
  assert len(body)==size and hashlib.sha256(body).hexdigest()==f['sha256'],'source digest changed'
  assert hashlib.new('sha1' if len(f['blob_sha'])==40 else 'sha256',b'blob '+str(size).encode()+b'\0'+body).hexdigest()==f['blob_sha']
  directory=os.dup(root)
  try:
   parts=f['path'].split('/')
   for part in parts[:-1]:
    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory);os.close(directory);directory=child
   fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
   try:
    before=os.fstat(fd); assert stat.S_ISREG(before.st_mode) and before.st_size==size
    with os.fdopen(os.dup(fd),'rb') as stream: actual=stream.read(limit+1)
    after=os.fstat(fd)
    named=os.stat(parts[-1],dir_fd=directory,follow_symlinks=False)
    assert (named.st_dev,named.st_ino,named.st_size,named.st_mtime_ns,named.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
    assert (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
    assert actual==body,'uncommitted source substitution'
   finally: os.close(fd)
  finally: os.close(directory)
  files.append({**f,'content_base64':base64.b64encode(body).decode(),'size_bytes':size})
 assert git('rev-parse','--verify','HEAD').decode().strip()==r['commit_sha'],'source HEAD changed'
 print(json.dumps({'method':r['method'],'commit_sha':r['commit_sha'],'files':files},sort_keys=True,separators=(',',':')))
finally: os.close(root)
'''
