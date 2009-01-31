#!/usr/bin/python
import os
import sys
import re
import shutil

from aws_secrets import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, PUBKEY, BUCKET

BIND_DIRS=["/boot"]
LVM_VOLS=["hydra/root"]
LVM_SNAPSHOT_SIZE="50G"

class path(str):
    def __div__(self, suffix):
        return path(os.path.join(self,suffix.lstrip('/')))
    def __rdiv__(self, prefix):
        return path(os.path.join(prefix,self.lstrip('/')))
    
    def __add__(self, suffix):
        return path(str.__add__(self,suffix))
    def __radd__(self, prefix):
        return path(str.__radd__(self,suffix))

    def basename(self):
        return path(os.path.basename(self))
    def dirname(self):
        return path(os.path.dirname(self))

HOME=path('/usr/local/backup')
MNT=HOME/'mnt'
DUMPS=HOME/'dumps'
AUX=HOME/aux
ARCHIVE=HOME/'.archive';

os.mkdir('/tmp/backup.lock')

try:
    # Process arguments
    VERBOSE=False
    for o in sys.argv[1:]:
        if o in ('-v','--verbose'):
            VERBOSE=True
        else:
            sys.stderr.write("usage: %s [-v]" % sys.argv[0])
            exit(1)

    def log(*args):
        if VERBOSE:
            for x in args:
                print x,
            print

    BIND_DIRS=[path(x) for x in BIND_DIRS]
    LVM_VOLS=[path(x) for x in LVM_VOLS]

    # Set the umask so that all files are only readable by root
    os.umask(077)

    def system(cmd):
        if VERBOSE:
            print sys.argv[0]+':',cmd
        err = os.system(cmd)
        if err:
            raise OSError, 'failed system command with code %s:\n%r' % (err,cmd)

    mount_re=re.compile('^[^ ]+ %s/(.*)(?: [^ ]+){4}\n' % re.escape(MNT))
    def mounts():
        return mount_re.finditer( open('/etc/mtab').read(), re.MULTILINE )

    def cleanup ():
        # log('Snapshot usage report...')
    #    df
    #     for fs in md0 md1 md2 md3 md4; do
    #         log Filesystem: /dev/$fs
    #         ps -o uid,pid,ppid,tty,time,args -p "$(fuser /dev/$fs 2>/dev/null)" 2>/dev/null
    #     done

        log('Unmounting backup directories...')
        for m in mounts():
            system('umount "%s"' % (MNT/m))

        log('Clearing LVM snapshots...')
        for v in LVM_VOLS:
            snap = '/dev'/ v+'.backup'
            if os.path.isdir(snap):
                system('/sbin/lvremove -f "%s"' % v+'.backup')

    cleanup()

    # create all snapshots as close together in time as possible
    log('Taking snapshots...')
    for v in LVM_VOLS:
        lvname=v.basename()+'.backup'
        system('/sbin/lvcreate -s -L %(LVM_SNAPSHOT_SIZE)r -n %(lvname)r %(v)r' % locals())

    log('Mounting snapshots...')
    for v in LVM_VOLS:
        mountpoint = MNT/'lvm'/v
        device = '/dev'/v+'.backup'
        os.makedirs(mountpoint)
        # the nouuid option is needed because all of my logical volumes are XFS
        system('mount %(device)r %(mountpoint)r -onouuid,ro' % locals())

    log('Binding un-snapshottable filesystems...')
    for d in BIND_DIRS:
        mountpoint = MNT/'bind'/d
        os.makedirs(mountpoint)
        system('mount --bind -oro %(d)r %(mountpoint)r' % locals())

    for dir in (DUMPS,AUX):
        system('rm -rf %r' % dir)
        os.makedirs(dir)

    MOUNTS=list(mounts())

    # log Dumping databases...
    # pg_dumpall -U pgsql > $HOME/dumps/postgresql.dump

    # log Dumping subversion repositories...
    # svnadmin hotcopy /usr/local/svn/main $HOME/dumps/subversion.main

    shutil.copyfile(HOME/'backup.sh', AUX/'backup.sh')

    log('Finding hard links...')
    for m in MOUNTS:
        os.makedirs(AUX/m)

    exclusions=[x.rstrip('\n') for x in open(HOME/'exclude.txt')]

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
        os.makedirs(ARCHIVE/d)

    os.environ['AWS_ACCESS_KEY_ID'] = AWS_ACCESS_KEY_ID
    os.environ['AWS_SECRET_ACCESS_KEY'] = AWS_SECRET_ACCESS_KEY

    VERBOSE_FLAG=VERBOSE and '-v4' or ''

    def duplicity(source_dir, target_dir, opts=''):
        archive_dir = ARCHIVE/target_dir
        uri = 's3+http://' + BUCKET/target_dir
        system(
            """duplicity --encrypt-key %(PUBKEY)s \
            --archive-dir %(archive_dir)r \
            %(VERBOSE_FLAG)s \
            %(opts)s \
            %(source_dir)r \
            %(uri)r"""
            % locals())

    def backup_mount (m, opts=''):
        # Added --exclude-other-filesystems just in case.  Somehow our LVM
        # snapshot of / contained all the files at the top level of the
        # /zorak zfs pool!
        duplicity(
            MNT/m, 
            'mnt'/m, 
            '--exclude-filelist %r --exclude-other-filesystems'  % (AUX/m/'exclude.txt')
            )

    for m in MOUNTS:
        backup_mount(m)

    duplicity(DUMPS, 'dumps')
    duplicity(AUX, 'aux')

finally:
    cleanup
    rm -rf /tmp/backup.lock

