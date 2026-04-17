FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Base dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    python3 \
    python3-pip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Gemini CLI — pre-installed so every sandbox starts ready, no install time
RUN npm install -g @google/gemini-cli

# Python dev tools
RUN pip3 install --no-cache-dir pytest pyrefly

# Git defaults for non-interactive commits inside sandbox
RUN git config --global user.email "grindbot@sandbox" \
    && git config --global user.name "GrindBot" \
    && git config --global init.defaultBranch main

WORKDIR /workspace
