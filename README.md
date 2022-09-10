# Homelab

## Overview

This repository, which is hosted on my private Gitlab instance and mirrored to Github, is used to deploy to and manage my Infrastructure. The primary tool used to do so is Ansible, which deploys to a more or less statically defined set of hosts. There are certainly ways I could get more creative with this--for one, I could pre-image the Pi's in my fleet using Packer; work out a way to deploy configuration changes to my managed switches (they don't have a native API for it sadly); and even consider using NFS shares for my Pi's to mount from. But for now this repo does exactly what it's designed to do: provide a practical way to document and modify my infrastructure, minimizing work and maximizing reliability in the long run.
