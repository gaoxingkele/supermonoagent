"""One-command local build, signing, verification and optional SSH publication."""
from ui_update_release import main

if __name__ == '__main__':
    main(publishing=True)
