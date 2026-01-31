# =============================================================================
# CI Runner Image - Pre-baked dependencies for fast pipelines
# =============================================================================
# Contains:
# - Ansible (from base image)
# - Python dependencies from requirements.txt
# - Ansible roles from ansible/requirements.yml
#
# Rebuild triggers: ci/Dockerfile, requirements.txt, ansible/requirements.yml
# =============================================================================

FROM willhallonline/ansible:latest

# Copy requirements files
COPY requirements.txt /tmp/requirements.txt
COPY ansible/requirements.yml /tmp/requirements.yml

# Install Python dependencies
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Pre-install Ansible roles to standard path
RUN ansible-galaxy install -r /tmp/requirements.yml -p /usr/share/ansible/roles

# Clean up
RUN rm /tmp/requirements.txt /tmp/requirements.yml

# Set Ansible to find pre-installed roles
ENV ANSIBLE_ROLES_PATH=/usr/share/ansible/roles
