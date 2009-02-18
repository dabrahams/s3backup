s3backup
========

Lots of tools exist out there that do backups to Amazon S3, but none
quite fit our needs.  In particular, though duplicity seemed to be
closest, it wouldn't recreate hard links upon restore.  

This system uses duplicity with an additional layer that allows us to
recreate hard links.

Installation on Debian/Ubuntu
-----------------------------

Download the duplicity sources.  As of this writing, the latest is `5.09`__

__ http://savannah.nongnu.org/download/duplicity/duplicity-0.5.09.tar.gz

On Debian/Ubuntu::

   $ tar xvf duplicity-*.tar.gz
   $ cd duplicity-*/
   $ sudo apt-get build-dep duplicity
   $ sudo aptitude install python-boto checkinstall
   $ sudo checkinstall python setup.py install
   $ cd ..

Hit return a few times, and duplicity will have been installed.  

.. Note:: I had a little trouble with earlier versions of duplicity.
   I'm not sure whether that was my fault, but the latest seems to be
   the greatest, too.

Now either click the download link at
http://github.com/techarcana/s3backup and unpack the result, or ::

  $ git-clone git://github.com/techarcana/s3backup.git

then

  $ sudo chown root:root s3backup
  $ sudo mv s3backup/etc/cron.d/* /etc/cron.d
  $ sudo cp s3backup/aws_secrets.py.sample aws_secrets.py
  $ sudo mv s3backup /usr/local

and edit your private information into ``/usr/local/s3backup/aws_secrets.py``.  Finally::

  $ sudo chmod 0600 /usr/local/s3backup/aws_secrets.py

That should do it!
