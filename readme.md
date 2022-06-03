# Objective

**pre-alpha level software and configuration, use at your own risk**

Ruchniux (Ruchnius + Linux) - a Linux filtering solution for the frum developer. That's the goal at least, I figure if a frum developer can get their work done even if a decent level of filtering, then this solution should work for other jobs / roles / needs.

This is a pretty ambitious project.

# Technology Stack

- E2Guardian
- Kosher MITM with SSL filtering
- (original target is towards Ubunutu 20 ish, but should theoretically work on any platform)

# Project Timeline

- A setup that works
- A one step user script that installs this on existing ubuntu platforms
- A Custom Linux distro with this setup out of the box

# How it works

- No Sudo
    * Will need some kind of restricted sudo access, but not sure what that looks like yet
- Use nix to install and manage most packages
    * Potentially create a linux package manager that will install to a user directory (similar to nix, but manually package some things that may be difficult to do in nix)
- E2Guardian preconfigured (I know that no single configuration works for everyone, but I'm going to try to get a config that works for the average software dev, and then provide ways to customize)
- Create a secondary user "ruchniux" that can be used to customize the setup. (provide an ssh tunnel for remote config, in case you are getting it setup by TAG)

# What is working so far

- [X] DNS Whitelist
- [X] E2Guardian
- [X] SSL transparent proxy with E2Guardian
- [ ] HTTP(S) Proxy configuration
    - [X] browser
    - [ ] system level
    - [ ] package managers
        - [ ] node
        - [ ] rust
        - [ ] python
        - [ ] nix
        - [ ] apt

# Guides
## Basic Whitelist
## Setting up Ruchniux
