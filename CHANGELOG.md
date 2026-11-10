# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project **only** adheres to the following _(as defined at [Semantic Versioning](https://semver.org/spec/v2.0.0.html))_:

> Given a version number MAJOR.MINOR.PATCH, increment the:
> 
> - MAJOR version when you make incompatible API changes
> - MINOR version when you add functionality in a backward compatible manner
> - PATCH version when you make backward compatible bug fixes

## [0.2.0] - 2026-11-10

### Changed

- Cleaned up layout and naming consistency (#43, #69, #70).
- Monitor all folders recursively (#53).
- Use iRODS logo for dock, app switcher, menu (#75).
- Moved recent activity to Log tab (#76).

### Removed

- Remove 'keep' mode, files are only moved, trashed, or deleted after upload (#31).

### Fixed

- Reject move-after-upload settings without a destination (#48).
- Fix Python 3.15 parallel uploads when Qt leaves a lazy executor unresolved (#72).

### Added

- Prevent monitoring ancestors and descendents of monitored folders (#33).
- Add visibility to queue (#38).
- Add multiple simultaneous uploads (#39).
- Walk newly monitored folder immediately (#49).
- Add Automatic mode for upload settings (#71).
- Add intermediate upload progress and ETA (#73).
- Add walk monitored folders on startup (#74).
- Add About tab with project information and latest release check (#77).
- Add automatic save for settings updates (#79).
- Add configurable threads per upload (#80).
- Add Throughput tab with graph of recent history (#81).

## [0.1.1] - 2026-09-16

Updated repository links in pyproject.toml.  No code changes.

## [0.1.0] - 2026-08-23

This is the first release of the iRODS Client System Tray application.
