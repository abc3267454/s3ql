#!/usr/bin/env python3
'''
t5_lock_rm.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

if __name__ == '__main__':
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import llfuse
import os.path
import s3ql.lock
import s3ql.remove
import sys
import t4_fuse

class LockRemoveTests(t4_fuse.fuse_tests):

    def runTest(self):
        self.mkfs()
        self.mount()
        self.tst_lock_rm()
        self.umount()
        self.fsck()

    def tst_lock_rm(self):

        # Extract tar
        tempdir = os.path.join(self.mnt_dir, 'lock_dir')
        filename = os.path.join(tempdir, 'myfile')
        os.mkdir(tempdir)
        with open(filename, 'w') as fh:
            fh.write('Hello, world')

        # copy
        try:
            s3ql.lock.main([tempdir])
        except:
            sys.excepthook(*sys.exc_info())
            self.fail("s3qllock raised exception")

        # Try to delete
        self.assertRaises(PermissionError, os.unlink, filename)

        # Try to write
        with self.assertRaises(PermissionError):
            open(filename, 'w+').write('Hello')

        # delete properly
        try:
            s3ql.remove.main([tempdir])
        except:
            sys.excepthook(*sys.exc_info())
            self.fail("s3qlrm raised exception")

        self.assertTrue('lock_dir' not in llfuse.listdir(self.mnt_dir))
