import re
import ast
from setuptools import setup, find_packages


_version_re = re.compile(r'__version__\s+=\s+(.*)')


with open('laika/__init__.py', 'rb') as f:
    version = str(ast.literal_eval(_version_re.search(
        f.read().decode('utf-8')).group(1)))

with open('requirements.txt') as f:
    requirements = f.read().split('\n')

with open('README.md') as f:
    readme_contents = f.read()


excel = ['xlrd==1.1.0', 'XlsxWriter==0.8.4']
query = ['SQLAlchemy==1.0.11']
postgres = query + ['psycopg2==2.6.1']
drive = ['PyDrive==1.3.1']
adwords = ['googleads==12.2.0']
s3 = ['boto3==1.4.3']
sftp = ['paramiko==2.0.1']

all_reports = excel + postgres + drive + adwords + s3 + sftp

test = ['mock==1.3.0']
docs = ['Sphinx>=1.7.1', 'sphinx-rtd-theme>=0.2.4']


setup(
    name='laika-lib',
    author='Seva Gavrilov',
    author_email='gavrilovseva@gmail.com',
    maintainer='Trocafone Data Science Team',
    maintainer_email='ds@trocafone.com',
    version=version,
    url='https://github.com/trocafone/laika',
    license='MIT',
    packages=find_packages(),
    description='A simple business reporting library',
    long_description=readme_contents,
    long_description_content_type='text/markdown',
    keywords='report reporting etl sql s3 drive ftp adwords',
    install_requires=requirements,
    include_package_data=True,
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6'
    ],
    tests_require=test,
    extras_require={
        'excel': excel,
        'query': query,
        'postgres': postgres,
        'drive': drive,
        'adwords': adwords,
        's3': s3,
        'sftp': sftp,

        'all_reports': all_reports,

        'test': test,
        'docs': docs
    },
    scripts=['scripts/laika.py']
)
