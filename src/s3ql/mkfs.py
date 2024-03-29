'''
mkfs.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from .logging import logging, setup_logging, QuietError
from . import CURRENT_FS_REV
from .backends.common import get_backend, BetterBackend, DanglingStorageURLError
from .backends import s3
from .common import get_backend_cachedir, CTRL_INODE, stream_write_bz2, PICKLE_PROTOCOL
from .database import Connection
from .metadata import dump_metadata, create_tables
from .parse_args import ArgumentParser
from getpass import getpass
from llfuse import ROOT_INODE
import os
import pickle
import shutil
import stat
import sys
import tempfile
import time
import atexit

log = logging.getLogger(__name__)

def parse_args(args):

    parser = ArgumentParser(
        description="Initializes an S3QL file system")

    parser.add_cachedir()
    parser.add_authfile()
    parser.add_debug_modules()
    parser.add_quiet()
    parser.add_ssl()
    parser.add_version()
    parser.add_storage_url()
    parser.add_fatal_warnings()

    parser.add_argument("-L", default='', help="Filesystem label",
                      dest="label", metavar='<name>',)
    parser.add_argument("--max-obj-size", type=int, default=10240, metavar='<size>',
                      help="Maximum size of storage objects in KiB. Files bigger than this "
                           "will be spread over multiple objects in the storage backend. "
                           "Default: %(default)d KiB.")
    parser.add_argument("--plain", action="store_true", default=False,
                      help="Create unencrypted file system.")
    parser.add_argument("--force", action="store_true", default=False,
                        help="Overwrite any existing data.")

    options = parser.parse_args(args)

    return options

def init_tables(conn):
    # Insert root directory
    timestamp = time.time()
    conn.execute("INSERT INTO inodes (id,mode,uid,gid,mtime,atime,ctime,refcount) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                   (ROOT_INODE, stat.S_IFDIR | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                   | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
                    os.getuid(), os.getgid(), timestamp, timestamp, timestamp, 1))

    # Insert control inode, the actual values don't matter that much 
    conn.execute("INSERT INTO inodes (id,mode,uid,gid,mtime,atime,ctime,refcount) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (CTRL_INODE, stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR,
                  0, 0, timestamp, timestamp, timestamp, 42))

    # Insert lost+found directory
    inode = conn.rowid("INSERT INTO inodes (mode,uid,gid,mtime,atime,ctime,refcount) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (stat.S_IFDIR | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                        os.getuid(), os.getgid(), timestamp, timestamp, timestamp, 1))
    name_id = conn.rowid('INSERT INTO names (name, refcount) VALUES(?,?)',
                         (b'lost+found', 1))
    conn.execute("INSERT INTO contents (name_id, inode, parent_inode) VALUES(?,?,?)",
                 (name_id, inode, ROOT_INODE))

def main(args=None):

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)
    setup_logging(options)

    if options.max_obj_size < 1024:
        # This warning should never be converrted to an exception
        log.warning('Warning: maximum object sizes less than 1 MiB will seriously degrade '
                 'performance.', extra={ 'force_log': True })

    try:
        plain_backend = get_backend(options, plain=True)
        atexit.register(plain_backend.close)
    except DanglingStorageURLError as exc:
        raise QuietError(str(exc))

    log.info("Before using S3QL, make sure to read the user's guide, especially\n"
             "the 'Important Rules to Avoid Loosing Data' section.")

    if isinstance(plain_backend, s3.Backend) and '.' in plain_backend.bucket_name:
        log.warning('***Warning*** S3 Buckets with names containing dots cannot be '
                    'accessed using SSL!')
        log.warning('(cf. https://forums.aws.amazon.com/thread.jspa?threadID=130560)')

        
    if 's3ql_metadata' in plain_backend:
        if not options.force:
            raise QuietError("Found existing file system! Use --force to overwrite")

        log.info('Purging existing file system data..')
        plain_backend.clear()
        log.info('Please note that the new file system may appear inconsistent\n'
                 'for a while until the removals have propagated through the backend.')

    if not options.plain:
        if sys.stdin.isatty():
            wrap_pw = getpass("Enter encryption password: ")
            if not wrap_pw == getpass("Confirm encryption password: "):
                raise QuietError("Passwords don't match.")
        else:
            wrap_pw = sys.stdin.readline().rstrip()
        wrap_pw = wrap_pw.encode('utf-8')

        # Generate data encryption passphrase
        log.info('Generating random encryption key...')
        fh = open('/dev/urandom', "rb", 0) # No buffering
        data_pw = fh.read(32)
        fh.close()

        backend = BetterBackend(wrap_pw, ('lzma', 2), plain_backend)
        backend['s3ql_passphrase'] = data_pw
        backend['s3ql_passphrase_bak1'] = data_pw
        backend['s3ql_passphrase_bak2'] = data_pw
        backend['s3ql_passphrase_bak3'] = data_pw
    else:
        data_pw = None

    backend = BetterBackend(data_pw, ('lzma', 2), plain_backend)
    atexit.unregister(plain_backend.close)
    atexit.register(backend.close)
    
    # Setup database
    cachepath = get_backend_cachedir(options.storage_url, options.cachedir)

    # There can't be a corresponding backend, so we can safely delete
    # these files.
    if os.path.exists(cachepath + '.db'):
        os.unlink(cachepath + '.db')
    if os.path.exists(cachepath + '-cache'):
        shutil.rmtree(cachepath + '-cache')

    log.info('Creating metadata tables...')
    db = Connection(cachepath + '.db')
    create_tables(db)
    init_tables(db)

    param = dict()
    param['revision'] = CURRENT_FS_REV
    param['seq_no'] = 1
    param['label'] = options.label
    param['max_obj_size'] = options.max_obj_size * 1024
    param['needs_fsck'] = False
    param['inode_gen'] = 0
    param['max_inode'] = db.get_val('SELECT MAX(id) FROM inodes')
    param['last_fsck'] = time.time()
    param['last-modified'] = time.time()

    # This indicates that the convert_legacy_metadata() stuff
    # in BetterBackend is not required for this file system.
    param['backend_revision'] = 1

    log.info('Dumping metadata...')
    with tempfile.TemporaryFile() as fh:
        dump_metadata(db, fh)
        def do_write(obj_fh):
            fh.seek(0)
            stream_write_bz2(fh, obj_fh)
            return obj_fh

        # Store metadata first, and seq_no second so that if mkfs
        # is interrupted, fsck won't see a file system at all.
        log.info("Compressing and uploading metadata...")
        obj_fh = backend.perform_write(do_write, "s3ql_metadata", metadata=param,
                                      is_compressed=True)
        backend.store('s3ql_seq_no_%d' % param['seq_no'], b'Empty')
        
    log.info('Wrote %.2f MiB of compressed metadata.', obj_fh.get_obj_size() / 1024 ** 2)
    with open(cachepath + '.params', 'wb') as fh:
        pickle.dump(param, fh, PICKLE_PROTOCOL)


if __name__ == '__main__':
    main(sys.argv[1:])
