"""Current component-based service manager. Legacy manager is retained for migration."""
from services_v2 import run
if __name__ == '__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('command',choices=['start','stop','status']);run(p.parse_args().command)
