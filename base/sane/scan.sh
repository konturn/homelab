#!/bin/sh
now=`date +"%Y-%m-%d-%H%M%S"`
/root/sane-scan-pdf/scan --device 'fujitsu:ScanSnap iX1600:144412' -r 600 -m Lineart -o /mnt/scan-${now}.pdf

