import os
import sys

from setuptools import setup

with open(os.path.join(os.path.abspath(os.path.dirname(__file__)), 'README.md'), encoding='utf-8') as fh:
    long_description = '\n' + fh.read()

if not sys.version_info[0] == 3 and sys.version_info[1] < 5:
    sys.exit('Python < 3.5 is not supported')

setup(
    name='steampy',
    version='1.0',
    license='MIT',
    author='SecorD',
    url='https://github.com/SecorD0/steampy',
    description='Python library for working with Steam',
    long_description_content_type='text/markdown',
    long_description=long_description,
    packages=['steampy', 'test'],
    install_requires=['requests', 'beautifulsoup4', 'rsa'],
    keywords=['steam', 'trade'],
    classifiers=[]
)
