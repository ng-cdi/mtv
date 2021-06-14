#!/usr/bin/env python

from mtv.net import VERSION
"Setuptools params"

from setuptools import setup, find_packages
from os.path import join

# Get version number from source tree
import sys
sys.path.append('.')

scripts = [join('bin', filename) for filename in ['mn']]

modname = distname = 'mtv'

setup(
    name=distname,
    version=VERSION,
    description='Testbed for Virtual Network Functions',
    author='Will Fantom',
    author_email='w.fantom@lancs.ac.uk',
    packages=['mtv'],
    long_description="""
        Testbed for Virtual Network Functions
        """,
    classifiers=[
        "License :: OSI Approved :: BSD License",
        "Programming Language :: Python",
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Topic :: System :: Emulators",
    ],
    keywords='networking emulator protocol Internet OpenFlow SDN NFV VNF',
    license='BSD',
    install_requires=[
        'setuptools'
    ],
    scripts=scripts,
)
