s3backup
========

Lots of tools exist out there that do backups to Amazon S3, but none
quite fit our needs.  In particular, though duplicity seemed to be
closest, it wouldn't recreate hard links upon restore.  

This system uses duplicity with an layer that allows us to recreate
hard links.
