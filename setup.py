import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="praw-wrapper",
    version="1.1.17",
    author="Watchful One",
    author_email="watchful@watchful.gr",
    description="A wrapper around PRAW for easier unit testing",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Watchful1/PrawWrapper",
    packages=setuptools.find_packages(),
    install_requires=["praw>=7.0.0"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
