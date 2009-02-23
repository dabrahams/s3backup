#!/usr/bin/python
import os
import glob
import sys
import re
import shutil
from subprocess import Popen, PIPE
import pathutils # for the shutils.rmtree on-error handler
import datetime
import time
from fnmatch import fnmatch

from aws_secrets import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, PUBKEY, BUCKET, PASSPHRASE

BIND_DIRS=['/boot']
LVM_VOLS=['mdRAID6/root']
LVM_SNAPSHOT_SIZE='5G'

ZFS_VOLS = [ 'zoreeba/home/*', 'zoreeba/home' ]

ZFS_SNAP_NAME='s3backup'

class path(str):
    def __div__(self, suffix):
        return path(os.path.join(self,suffix.lstrip('/')))

    def __rdiv__(self, prefix):
        return path(os.path.join(prefix,self.lstrip('/')))
    
    def __add__(self, suffix):
        return path(str.__add__(self,suffix))

    def basename(self):
        return path(os.path.basename(self))

    def dirname(self):
        return path(os.path.dirname(self))

HOME=path('/usr/local/s3backup')
MNT=HOME/'mnt'
DUMPS=HOME/'dumps'
AUX=HOME/'aux'
ARCHIVE=HOME/'.archive';
ZFS_POOLS=set( [x.split('/',1)[0] for x in ZFS_VOLS ] )

BIND_DIRS=[path(x) for x in BIND_DIRS]
LVM_VOLS=[path(x) for x in LVM_VOLS]

VERBOSE=False
DRY_RUN=False
DRY_REMOTE=False
def system(cmd, dry = None):
    if VERBOSE:
        log(sys.argv[0]+':',cmd)

    if dry or dry is None and DRY_RUN: return

    p = Popen(cmd, stdout=PIPE, stderr=PIPE, shell=True)

    stdout,stderr = p.communicate()

    if VERBOSE:
        if stdout:
            log('stdout: ', stdout)
        if stderr:
            log('stderr: ', stderr)

    err = p.returncode

    if err:
        raise OSError, 'failed system command with returncode %s:\n%r\n%s' % (err,cmd,stderr)

mount_re=re.compile('^[^ ]+ %s/(.*)(?: [^ ]+){4}\n' % re.escape(MNT), re.MULTILINE)
def mounts():
    return mount_re.findall( open('/etc/mtab').read() )

def cleanup(preamble = False):
    # log('Snapshot usage report...')
#    df
#     for fs in md0 md1 md2 md3 md4; do
#         log Filesystem: /dev/$fs
#         ps -o uid,pid,ppid,tty,time,args -p "$(fuser /dev/$fs 2>/dev/null)" 2>/dev/null
#     done

    existing = preamble and 'pre-existing ' or ''

    MOUNTS = mounts()

    # Reverse sort so child directories get unmounted before parents
    MOUNTS.sort(reverse=True)
    for m in MOUNTS:
        log('Unmounting %sbackup directory %s...' % (existing,MNT/m))
        system('umount "%s"' % (MNT/m))

    for v in LVM_VOLS:
        snap = '/dev'/ v+'.backup'
        if os.path.exists(snap):
            log('Clearing %sLVM snapshot %s...' % (existing, snap))
            system('/sbin/lvremove -f "%s"' % v+'.backup')

    if not preamble:
        for p in ZFS_POOLS:
            zfs_destroy_snapshot(p)

    def clean(path):
        if (os.path.exists(path)):
            log('Cleaning up %s%s tree...' % (existing, os.path.split(path)[1]))
            shutil.rmtree(path, onerror=pathutils.onerror)

    clean(MNT)
    clean(AUX)
    clean(DUMPS)

    if zfs_fuse_is_running():
        zfs_fuse_clean_emulated_snapshot_mounts()

def duplicity(target_dir, command = '', tail_args = '', verbose=None):
    if verbose is None:
        verbose = VERBOSE and '-v4' or ''

    uri = 's3+http://' + BUCKET + '/' + target_dir
    try:
        system('duplicity %(command)s %(verbose)s %(uri)r %(tail_args)s' % locals(), DRY_REMOTE)
    except:
        # Attempt to clean up extraneous duplicity files
        os.environ['PASSPHRASE'] = PASSPHRASE
        try: 
            system('duplicity cleanup %(verbose)s %(uri)r' % locals(), DRY_REMOTE)
        except: 
            pass
        finally: 
            del os.environ['PASSPHRASE']
        raise

            

def duplicity_backup(source_dir, target_dir, opts='', verbose=None):
    archive_dir = ARCHIVE/target_dir
    require_dirs(archive_dir)
    pubkey = PUBKEY
    duplicity(
        target_dir=target_dir, 
        command = '--encrypt-key %(pubkey)s --archive-dir %(archive_dir)r'
                  ' --full-if-older-than 1M %(opts)s %(source_dir)r' % locals(), 
        verbose=verbose)

def backup_mount (m, opts=''):
    duplicity_backup(
        MNT/m, 
        'mnt'/path(m), 
        '--exclude-filelist %r --exclude-other-filesystems'  % (AUX/m/'exclude.txt')
        )

def log(*args):
    if VERBOSE:
        for x in args:
            print x,
        print
        sys.stdout.flush()

def require_dirs(path):
    try:
        os.makedirs(path)
    except OSError, e:
        if e.errno != 17: raise # ignore 'directory exists' errors

def mount_bind(src, dst):
    log('mount_bind', src, dst)
    mountpoint = MNT/dst
    log('mount_bind creating mountpoint', mountpoint)
    require_dirs(mountpoint)
    system('mount --bind %(src)r %(mountpoint)r' % locals())

#
# ZFS utilities
#
def zfs_snapshot_name(pool):
    return pool+'@'+ZFS_SNAP_NAME

def zfs_snapshot_path(pool, rooted = False):
    return path('/') / pool / '.zfs' / 'snapshot' / ZFS_SNAP_NAME

# Is there a ZFS-FUSE daemon running?
_zfs_fuse_is_running_=None # initially unknown
def zfs_fuse_is_running():
    global _zfs_fuse_is_running_
    if _zfs_fuse_is_running_ is not None:
        return _zfs_fuse_is_running_

    number = re.compile(r'\d*$')
    stat_fields = re.compile(
        r'\s+'.join([
            r'(?P<pid>\d+)', 
            r'\((?P<comm>.+)\)',
            r'(?P<state>[RSDZTW])',
            r'(?P<ppid>\d+)'
            ]))

    if os.path.isdir('/proc'):
        for pid in os.listdir('/proc'):
            if not number.match(pid): continue
            stat = stat_fields.match(open('/proc/%s/stat' % pid).read())
            if stat.group('comm') == 'zfs-fuse':
                _zfs_fuse_is_running_=True
                return True

    _zfs_fuse_is_running_=False
    return False

def zfs_fuse_create_emulated_snapshot_mounts():
    log('Creating zfs-fuse clones to emulate mounted snapshots')

    for match in zfs_list():
        zpath, snap = match.group('zpath','snap')

        if snap == ZFS_SNAP_NAME and zpath.find('/.zfs') == -1 \
                and zfs_path_needs_backup(zpath):
            system('zfs clone -p %r -o readonly=on %r' % (
                    zpath+'@'+ZFS_SNAP_NAME, zpath/ZFS_SNAP_RELPATH))

def zfs_fuse_clean_emulated_snapshot_mounts():
    log('Clearing any empty clones to emulate mounted snapshots')

    zpaths = [ match.group('zpath') for match in zfs_list() ]
    zpaths.sort(reverse=True)
    pat = re.compile(r'^.*/.zfs(/snapshot)?/?$')
    for p in zpaths:
        if pat.match(p):
            # Ignore any errors we might get.  These filesystems don't
            # hurt anyone; they're just needless clutter
            try: zfs_destroy(p)
            except: pass

zlist_parse = re.compile(
    r'^(?P<zpath>[^@\s]+)(?:@(?P<snap>\S+))?\s+(?P<used>\S+)\s+(?P<avail>\S+)\s+(?P<refer>\S+)\s+(?P<mountpoint>.*)', re.MULTILINE)

def zfs_list():
    return zlist_parse.finditer(Popen('zfs list'.split(), stdout=PIPE).communicate()[0])

# zfs destroy seems to fail often with "dataset is busy" but always
# works if we retry; this little wrapper gets us around that problem
def zfs_destroy(zpath, options=[]):
    command = ['zfs', 'destroy'] + options + [zpath]
    if VERBOSE:
        log(sys.argv[0]+':', ' '.join(repr(x) for x in command));

    if DRY_RUN: return

    p = Popen(command, stdout=PIPE, stderr=PIPE)
    stdout,stderr = p.communicate()
    if p.returncode and stderr.find('dataset is busy') != -1:
        log('dataset is busy...retrying')
        time.sleep(1)
        p = Popen(command, stdout=PIPE, stderr=PIPE)
        stdout, stderr = p.communicate()
        if not p.returncode:
            log('success!')
    if p.returncode:
        raise OSError, 'failed system command with returncode %s:\n%r\n%s' % (p.returncode,cmd,stderr)

def zfs_destroy_snapshot(pool):
    return zfs_destroy(zfs_snapshot_name(pool), options=['-R','-f'])

def zfs_path_needs_backup(zpath):
    for pat in ZFS_VOLS:
        if fnmatch(zpath, pat):
            return True
    return False

ZFS_SNAP_RELPATH = path('.zfs/snapshot')/ZFS_SNAP_NAME
# 
# Let's get started   
#
def run():
    os.mkdir('/var/lock/s3backup')
    try:
        # Set the umask so that all files are only readable by root
        os.umask(077)

        cleanup(preamble=True)

        try:
            # create all snapshots as close together in time as possible
            log('Taking snapshots of ZFS pools...')
            for p in ZFS_POOLS:
                system('zfs snapshot -r %r' % zfs_snapshot_name(p))

            log('Taking snapshots of LVM volumes...')
            for v in LVM_VOLS:
                lvname=v.basename()+'.backup'
                size = LVM_SNAPSHOT_SIZE
                system('/sbin/lvcreate -s -L %(size)r -n %(lvname)r %(v)r' % locals())

            if zfs_fuse_is_running():
                zfs_fuse_create_emulated_snapshot_mounts()

            log('Mounting snapshots...')
            for v in LVM_VOLS:
                mountpoint = MNT/'lvm'/v
                device = '/dev'/v+'.backup'
                log('Creating mountpoint:', mountpoint)
                os.makedirs(mountpoint)

                # the nouuid option is needed because all of my logical volumes are XFS
                log('Mounting device:', device)
                system('mount %(device)r %(mountpoint)r -onouuid,ro' % locals())

            if ZFS_POOLS:
                log('Binding ZFS snapshots...')
                for match in zfs_list():
                    zpath, snap, zfs_mountpoint = match.group('zpath','snap','mountpoint')
                    if zfs_mountpoint.endswith(ZFS_SNAP_RELPATH) \
                            and zfs_path_needs_backup(zpath):

                        # In case of zfs-fuse workaround, drop
                        # additional cruft from zpath
                        mount_path = zpath.rsplit(ZFS_SNAP_RELPATH,1)[0]
                        mount_bind(zfs_mountpoint, path('zfs')/mount_path)

            log('Binding un-snapshottable filesystems...')
            for d in BIND_DIRS:
                mount_bind(d, path('bind')/d)

            for dir in (DUMPS,AUX):
                os.makedirs(dir)

            MOUNTS=mounts()

            # log Dumping databases...
            # pg_dumpall -U pgsql > $HOME/dumps/postgresql.dump

            # log Dumping subversion repositories...
            # svnadmin hotcopy /usr/local/svn/main $HOME/dumps/subversion.main

            shutil.copyfile(HOME/'backup.py', AUX/'backup.py')

            log('Finding hard links...')
            for m in MOUNTS:
                os.makedirs(AUX/m)

            if os.path.exists(HOME/'exclude.txt'):
                exclusions=[x.rstrip('\n') for x in open(HOME/'exclude.txt')]
            else:
                exclusions=[]

            find_prune=' '.join(['-wholename %r -prune -o'%(MNT/x) for x in exclusions])
            for m in MOUNTS:
                mountpoint = MNT/m
                linkfile = AUX/m/'links.txt'
                system(
                    'find %(mountpoint)r %(find_prune)s -type f -links +1 -print0 | xargs -r -0 ls -li | sort > %(linkfile)r'
                    % locals()
                    )

            log('Creating exclude lists...')
            inode_re = re.compile('^ *([^ ]+)(?: +[^ ]+){7} +(.*)$')
            for m in MOUNTS:
                mx = [MNT/x for x in exclusions if x.startswith(m+'/')]
                linkfile = AUX/m/'links.txt'

                prev=None
                for inode,pathname in inode_re.finditer(open(linkfile).read(), re.M):
                    if inode == prev:
                        mx.append(pathname)
                    prev = inode

                log('Exclusions for %s: %s' % (m,mx))
                open(AUX/m/'exclude.txt','w').write('\n'.join(mx))

            log('Creating archive directories...')
            for d in MOUNTS + ['aux', 'dumps']:
                require_dirs(ARCHIVE/d)

            # Set up $HOME so there's no chance of confusing gpg.  When
            # people use sudo it normally doesn't change $HOME to
            # correspond to the target user, but then gpg looks for keys
            # in the wrong places.  Also, expanduser('~') just looks at
            # the value of $HOME, so you need to explicitly request the
            # current user's home directory
            os.environ['HOME'] = os.path.expanduser('~' + os.environ['USER'])

            os.environ['AWS_ACCESS_KEY_ID'] = AWS_ACCESS_KEY_ID
            os.environ['AWS_SECRET_ACCESS_KEY'] = AWS_SECRET_ACCESS_KEY

            for m in MOUNTS:
                backup_mount(m)

            duplicity_backup(DUMPS, 'dumps')
            duplicity_backup(AUX, 'aux')

            log('Removing outdated backups...')
            os.environ['PASSPHRASE'] = PASSPHRASE
            for target in [MNT/m for m in MOUNTS] + [DUMPS, AUX]:
                duplicity(target, command = 'remove-older-than 3M')

            log('Backup succeeded')
        except:
            # cleanup actions can sometimes obscure the location of
            # the error, since it gets reported when the exception
            # leaves the app; this tends to help clarify things.
            log('************* ERROR OCCURRED HERE ****************')
            raise
        finally:
            cleanup()
    finally:
        os.rmdir('/var/lock/s3backup')

if __name__ == '__main__':
    # Process arguments
    for o in sys.argv[1:]:
        if o in ('-v','--verbose'):
            VERBOSE=True
        elif o in ('-n','--dry-run'):
            DRY_RUN=True
            DRY_REMOTE=True
        elif o in ('-N','--no-backup'):
            DRY_REMOTE=True
        else:
            sys.stderr.write('usage: %s [-v] [-n] [-N]' % sys.argv[0])
            exit(1)
    run()
