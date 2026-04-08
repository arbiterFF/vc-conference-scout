#!/bin/bash
cd "$(dirname "$0")" && python3 server.py & open http://localhost:8081
