
## L6 apply of BIOS black-screen fix (debian-bootc)
Cause proven (see tk note L6). Fix ready: export WLR_RENDERER=pixman in debian-bootc src/buildisopreinstall/var/home/live/.bash_profile + tests/unit/test_live_installer_renderer.py.
Blocker: writes in debian-bootc denied by tk gate, no active ticket; backend cannot create one.
Command: cd debian-bootc && tk start L6
Output: tk: github graphql: Resource not accessible by integration
Command: tk init --force -> already initialised (github)
Needs: working github token/permission for debian-bootc tk (owner).
