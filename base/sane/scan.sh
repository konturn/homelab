#!/bin/sh
now=`date +"%Y-%m-%d-%H%M"`
/root/sane-scan-pdf/scan --device 'fujitsu:ScanSnap iX1600:144412' -d -r 300 -v -m Lineart --skip-empty-pages -o /root
