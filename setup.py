from setuptools import setup, find_packages

setup(
	name="girc",
	version="1.0.0",
	author="ekimekim",
	author_email="mikelang3000@gmail.com",
	description="gevent based irc library",
	url="https://github.com/ekimekim/girc",
	download_url = 'https://github.com/ekimekim/girc/tarball/1.0.0',
	packages=find_packages(),
	install_requires=[
		'gevent',
		'monotonic',
	],
)
