# SPDX-License-Identifier: GPL-3.0-or-later
"""Backup upload (authenticated route only). The archive is fully verified before it is kept."""
import os, json, re, stat, secrets, tempfile, zipfile
from pathlib import Path
from datetime import datetime,timezone
from email import policy
from email.parser import BytesParser

MAX_UPLOAD_ZIP=16*1024*1024

def upload_backup(config, handler):
    import system as S
    ct=handler.headers.get('Content-Type','')
    if not ct.lower().startswith('multipart/form-data;') or len(ct)>250:
        raise ValueError('Expected multipart upload')
    try: size=int(handler.headers.get('Content-Length','0'))
    except ValueError: raise ValueError('Invalid upload length') from None
    if not 0<size<=MAX_UPLOAD_ZIP+8192: raise ValueError('Upload too large')
    payload=handler.rfile.read(size)
    if len(payload)!=size: raise ValueError('Incomplete upload')
    # Construct proper MIME headers using CRLF, not literal backslash characters.
    header=b'MIME-Version: 1.0\r\nContent-Type: '+ct.encode('ascii')+b'\r\n\r\n'
    msg=BytesParser(policy=policy.default).parsebytes(header+payload)
    if not msg.is_multipart(): raise ValueError('Invalid multipart upload')
    parts=list(msg.iter_parts())
    if len(parts)!=2: raise ValueError('Exactly two form fields required')
    fields={}
    for p in parts:
        key=p.get_param('name',header='Content-Disposition')
        if p.get_content_disposition()!='form-data' or key not in ('csrf','archive') or key in fields:
            raise ValueError('Unexpected upload form field')
        fields[key]=p
    if set(fields)!=set(('csrf','archive')) or fields['csrf'].get_filename() is not None:
        raise ValueError('Invalid upload form')
    token=fields['csrf'].get_payload(decode=True)
    if not isinstance(token,bytes) or len(token)>128: raise ValueError('Invalid token')
    try: token=token.decode('ascii')
    except UnicodeError: raise ValueError('Invalid token') from None
    filename=fields['archive'].get_filename()
    if not isinstance(filename,str) or not filename.lower().endswith('.zip'):
        raise ValueError('Only ZIP files are allowed')
    data=fields['archive'].get_payload(decode=True)
    if not isinstance(data,bytes) or not 0<len(data)<=MAX_UPLOAD_ZIP:
        raise ValueError('Invalid archive length')
    if not S._consume_token(token): raise ValueError('Expired or replayed token')
    root=S._backup_dir(config,create=True)
    with S.LOCK:
        if len(S._files(config))>=20: raise ValueError('Backup limit reached')
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(prefix='.themearr-upload-',suffix='.zip',dir=root,delete=False) as f:
                temporary=Path(f.name)
                f.write(data);f.flush();os.fsync(f.fileno())
            S.verify_archive(temporary)
            for _ in range(8):
                name='tsbackup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4)+'.zip'
                try: os.link(temporary,root/name,follow_symlinks=False);break
                except FileExistsError: continue
            else: raise ValueError('Could not assign an archive name')
            return name
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)
