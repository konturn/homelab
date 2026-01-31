# =============================================================================
# Custom CI Image - Pre-baked dependencies for zero-install pipeline jobs
# =============================================================================
# This image contains all Python packages and Ansible roles pre-installed,
# so CI jobs only need to set up SSH keys and run playbooks.
#
# Rebuild trigger: Update CI_IMAGE_VERSION in .gitlab-ci.yml when deps change
# =============================================================================

FROM willhallonline/ansible:latest

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# Install Ansible roles
COPY ansible/requirements.yml /tmp/requirements.yml
RUN mkdir -p /opt/ansible-roles && \
    ansible-galaxy install -r /tmp/requirements.yml -p /opt/ansible-roles && \
    rm /tmp/requirements.yml

# Set Ansible roles path
ENV ANSIBLE_ROLES_PATH=/opt/ansible-roles
