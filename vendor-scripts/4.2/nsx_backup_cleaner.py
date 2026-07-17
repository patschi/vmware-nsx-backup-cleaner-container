#!/usr/bin/env python
# ***************************************************************************
# Copyright 2020-2021 VMware, Inc.  All rights reserved. VMware Confidential.
# ***************************************************************************
# The purpose of this script is to remove old NSX backup files. Typically, this script
# will be placed on the SFTP server where the NSX Manager is uploading backup files,
# and included into a scheduler, for example cron.  Before running this script, you
# should update the BACKUP_ROOT variable.  This script works on Linux and Windows with
# both Python 2 and Python 3.
#
# On Linux SFTP server:
# You can add this script in the crontab to automatically run this script once daily
# Edit the anacron at /etc/cron.d or use crontab -e and add following line to execute the script at 10am everyday
# 00 10 * * * /sbin/nsx_backup_cleaner.py
#
# On Windows SFTP server:
# schtasks /Create /SC DAILY /TN PythonTask /TR "PATH_TO_PYTHON_EXE PATH_TO_PYTHON_SCRIPT"
# or you can add the same in TaskScheduler



from stat import S_ISREG, ST_ATIME, ST_CTIME, ST_MODE, S_ISDIR, S_IWUSR
import os, sys, time, datetime, shutil, getopt

def delete_files(delete_path_list, count):
    deleted_files = []

    for file in delete_path_list:
        for root, dirs, files in os.walk(file):
            for fname in files:
                full_path = os.path.join(root, fname)
                os.chmod(full_path, S_IWUSR)
        if count > 0:
            deleted_files.append(file)
            if os.path.isdir(file):
                shutil.rmtree(file)
            else:
                os.remove(file)
            count = count - 1
    return deleted_files


def delete_old_backup_enteries(folder, keep_days, min_count):
    keep_files = []
    for elem in os.listdir(folder):
        paths_sorted = []
        entries1 = (os.path.join(folder, elem, fn) for fn in os.listdir(os.path.join(folder, elem)))
        entries2 = ((os.stat(path), path) for path in entries1)
        entries3 = ((stat[ST_CTIME], path) for stat, path in entries2)
        for cdate, path in sorted(entries3):
            paths_sorted.append(path)

        if (len(paths_sorted) <= min_count):
            for file in paths_sorted:
                keep_files.append(file)
            continue

        delete_path_list = []
        for path in paths_sorted:
            file_create_time = os.path.getmtime(path)
            time_now = time.time()
            if ((time_now - file_create_time) > (keep_days * 24 * 60 * 60)):
                delete_path_list.append(path)

        deleted_files = delete_files(delete_path_list, min(len(delete_path_list), len(paths_sorted) - min_count))
        for file in deleted_files:
            paths_sorted.remove(file)

        for file in paths_sorted:
            keep_files.append(file)

    print(("Keeping the following backup files for folder %s" % folder))
    for file in keep_files:
        print(file)

def usage():
    print("""\
    Usage: nsx_backup_cleaner.py -d backup_dir [-k 1] [-l 5] [-h]
           Or
           nsx_backup_cleaner.py --dir backup_dir [--retention-period 1] [--min-count 5] [--help]

           Required
               -d/--dir: Backup root directory
               -k/--retention-period: Number of days need to retain a backup file
           Optional
               -l/--min-count: Minimum number of backup files to be kept, default value is 100
               -h/--help: Display help message
           """)
def main():
    BACKUP_ROOT = None

    BACKUPS_KEEP_DAYS = None
    # Minimum allowed: 100
    BACKUPS_MINCOUNT = 100

    try:
        opts, args = getopt.getopt(sys.argv[1:], "hd:k:l:", ["dir=", "retention-period=", "min-count=", "help"])
    except getopt.GetoptError as err:
    # print help information and exit:
        print ((str(err))) # will print something like "option -a not recognized"
        usage()
        sys.exit()

    for opt, arg in opts:
        if opt in ("-h", "--help"):
            usage()
            sys.exit()
        elif opt in ("-d", "--dir"):
            BACKUP_ROOT = arg
        elif opt in("-k", "--retention-period"):
            BACKUPS_KEEP_DAYS = int(arg)
        elif opt in ("-l", "--min-count"):
            BACKUPS_MINCOUNT = int(arg)
        else:
            usage()
            sys.exit()

    if (BACKUP_ROOT == None):
        print("Missing Backup Root")
        usage()
        sys.exit()

    if (BACKUPS_KEEP_DAYS == None):
        print("Missing Backup Retention Period in number of days")
        usage()
        sys.exit()

    if (not os.path.isdir(BACKUP_ROOT)):
        print("Wrong backup root directory")
        usage()
        sys.exit()

    backup_dirs = os.listdir(BACKUP_ROOT)
    invalid_dir = True
    for elem in backup_dirs:
        if (elem in ["nsx-bkp"]):
            delete_old_backup_enteries(os.path.join(BACKUP_ROOT, 'nsx-bkp'), BACKUPS_KEEP_DAYS,
                                       BACKUPS_MINCOUNT)
            invalid_dir = False
        if (elem in ["nsx-inv"]):
            delete_old_backup_enteries(os.path.join(BACKUP_ROOT, 'nsx-inv'), BACKUPS_KEEP_DAYS,
                                       BACKUPS_MINCOUNT)
            invalid_dir = False
    if invalid_dir:
        print ("Cleanup script works only in folders, that contains subfolders \"nsx-bkp\" and \"nsx-inv\"")


if __name__ == "__main__":
    main()

