from setuptools import setup, find_packages

setup(name="girc",
      version="0.0.1",
      author="ekimekim",
      author_email="mikelang3000@gmail.com",
      description="gevent based irc library",
      packages=find_packages(),
      install_requires=[
          'gevent',
          'monotonic',
      ],
     )
