#!/bin/sh
# Package the plugin into bin/calibre-ridibooks-plugin.zip
# (No vendored libs anymore - the plugin uses calibre's own browser.)
rm -f bin/calibre-ridibooks-plugin.zip
zip -r bin/calibre-ridibooks-plugin.zip *.py *.txt translations/*
