#!/bin/bash

# Remove existing package if it exists
rm -f Goodvibes.sublime-package

# Create the package by zipping the required files
zip Goodvibes.sublime-package \
    .python-version \
    goodvibes.py \
    goodvibes.sublime-settings \
    Default.sublime-commands \
    Context.sublime-menu \
    Default.sublime-keymap

echo "Created Goodvibes.sublime-package"