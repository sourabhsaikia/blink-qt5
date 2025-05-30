#!/bin/bash

# Install C building dependencies
echo "Installing port dependencies..."
sudo port install yasm x264 gnutls openssl sqlite3 ffmpeg mpfr libmpc libvpx wget

RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo
    echo "Failed to install all C dependencies"
    echo
    exit 1
fi

# Install Python building dependencies
echo "Installing python dependencies..."

pip3 install --upgrade pip
pip3 install --user cython==0.29.37 dnspython lxml twisted python-dateutil greenlet zope.interface requests gmpy2 wheel gevent

RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo
    echo "Failed to install all python dependencies"
    echo
    exit 1
fi

# Create a work directory

if [ ! -d work ]; then
    mkdir work
fi

cd work

# Download and build SIP SIMPLE client SDK built-in dependencies
for p in python3-application python3-eventlib python3-gnutls python3-otr python3-msrplib python3-xcaplib; do
    if [ ! -d $p ]; then
        darcs clone --lazy http://devel.ag-projects.com/repositories/$p
    fi
    cd $p
    echo "Installing $p..."
    pip3 install --user .

    if [ $RESULT -ne 0 ]; then
        echo
        echo "Failed to install $p dependency"
        cd ..
        echo
        exit 1
        fi

    cd ..

done

# Download and build SIP SIMPLE client SDK
if [ ! -d python3-sipsimple ]; then
    darcs clone --lazy http://devel.ag-projects.com/repositories/python3-sipsimple
fi

cp ../_sipsimple_codecs.py python3-sipsimple/sipsimple/configuration/_codecs.py

echo "Installing SIP Simple SDK..."
cd python3-sipsimple
chmod +x ./get_dependencies.sh
./get_dependencies.sh 

if [ $RESULT -ne 0 ]; then
    echo
    echo "Failed to install all SIP SIMPLE SDK dependencies"
    echo
    exit 1
fi

pip3 install --user .
if [ $RESULT -ne 0 ]; then
    echo
    echo "Failed to build SIP SIMPLE SDK"
    echo
    cd
    exit 1
fi

cd ..

if [ ! -d sipclients3 ]; then
    darcs clone --lazy http://devel.ag-projects.com/repositories/sipclients3
fi

cd sipclients3
pip3 install --user .
cd ..

