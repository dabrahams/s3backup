#!/usr/bin/python
import os
import sys
import re
import shutil
import subprocess
import pathutils # for the shutils.rmtree on-error handler
import datetime

from aws_secrets import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, PUBKEY, BUCKET, PASSPHRASE

BIND_DIRS=['/boot']
LVM_VOLS=['raid6/root']
LVM_SNAPSHOT_SIZE='5G'

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

VERBOSE=False
def system(cmd):
    if VERBOSE:
        log(sys.argv[0]+':',cmd)
    p = subprocess.Popen(cmd, shell=True)
    p.wait()
    err = p.returncode
    if err:
        raise OSError, 'failed system command with returncode %s:\n%r' % (err,cmd)

mount_re=re.compile('^[^ ]+ %s/(.*)(?: [^ ]+){4}\n' % re.escape(MNT), re.MULTILINE)
def mounts():
    return mount_re.findall( open('/etc/mtab').read() )

def cleanup ():
    # log('Snapshot usage report...')
#    df
#     for fs in md0 md1 md2 md3 md4; do
#         log Filesystem: /dev/$fs
#         ps -o uid,pid,ppid,tty,time,args -p "$(fuser /dev/$fs 2>/dev/null)" 2>/dev/null
#     done

    MOUNTS = mounts()
    log('Unmounting backup directories %s...' % MOUNTS)
    for m in MOUNTS:
        system('umount "%s"' % (MNT/m))
        os.rmdir(MNT/m)

    log('Clearing LVM snapshots %s...' % LVM_VOLS)
    for v in LVM_VOLS:
        snap = '/dev'/ v+'.backup'
        if os.path.exists(snap):
            system('/sbin/lvremove -f "%s"' % v+'.backup')

    def clean(path):
        if (os.path.exists(path)):
            log('Cleaning up existing %s tree...' % os.path.split(path)[1])
            shutil.rmtree(path, onerror=pathutils.onerror)

    clean(MNT)
    clean(AUX)
    clean(DUMPS)

def duplicity(target_dir, command = '', verbose=VERBOSE and '-v4' or ''):
    uri = 's3+http://' + BUCKET + '/' + target_dir
    try:
        system('duplicity %(command)s %(verbose)s %(uri)r' % locals())
    except:
        # Attempt to clean up extraneous duplicity files
        os.environ['PASSPHRASE'] = PASSPHRASE
        try: 
            system('duplicity cleanup %(verbose)s %(uri)r' % locals())
        except: 
            pass
        finally: 
            del os.environ['PASSPHRASE']
        raise
            

def duplicity_backup(source_dir, target_dir, opts='', verbose=VERBOSE and '-v4' or ''):
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

os.mkdir('/var/lock/s3backup')
try:
    # Process arguments
    for o in sys.argv[1:]:
        if o in ('-v','--verbose'):
            VERBOSE=True
        else:
            sys.stderr.write('usage: %s [-v]' % sys.argv[0])
            exit(1)

    BIND_DIRS=[path(x) for x in BIND_DIRS]
    LVM_VOLS=[path(x) for x in LVM_VOLS]

    # Set the umask so that all files are only readable by root
    os.umask(077)

    cleanup()

    try:
        # create all snapshots as close together in time as possible
        log('Taking snapshots...')
        for v in LVM_VOLS:
            lvname=v.basename()+'.backup'
            system('/sbin/lvcreate -s -L %(LVM_SNAPSHOT_SIZE)r -n %(lvname)r %(v)r' % locals())

        log('Mounting snapshots...')
        for v in LVM_VOLS:
            mountpoint = MNT/'lvm'/v
            device = '/dev'/v+'.backup'
            log('Creating mountpoint:', mountpoint)
            os.makedirs(mountpoint)

            # the nouuid option is needed because all of my logical volumes are XFS
            log('Mounting device:', device)
            system('mount %(device)r %(mountpoint)r -onouuid,ro' % locals())

        log('Binding un-snapshottable filesystems...')
        for d in BIND_DIRS:
            mountpoint = MNT/'bind'/d
            os.makedirs(mountpoint)
            system('mount --bind -oro %(d)r %(mountpoint)r' % locals())

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

            open(AUX/m/'exclude.txt','w').write('\n'.join(mx))

        log('Creating archive directories...')
        for d in MOUNTS + ['aux', 'dumps']:
            require_dirs(ARCHIVE/d)

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
        log('*** Error ***')
        raise
    finally:
        cleanup()
finally:
    os.rmdir('/var/lock/s3backup')

