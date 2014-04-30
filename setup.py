from setuptools import setup, find_packages

setup(name="geventirc",
      version="0.1dev",
      author="Antonin Amand (@gwik)",
      author_email="antonin.amand@gmail.com",
      description="gevent based irc client",
      packages=find_packages(),
      install_requires=[
          'gevent',
      ],
     )
