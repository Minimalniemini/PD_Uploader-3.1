"""Windows-only isolated Edge window smoke test. No login, uploads or external pages."""
import os
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from control_room_uploader.sessions import SessionPool, ManagedSession


def window_state(context,page):
    client=context.new_cdp_session(page)
    try:
        ident=client.send('Browser.getWindowForTarget')['windowId']
        return client.send('Browser.getWindowBounds',{'windowId':ident})['bounds']['windowState']
    finally:client.detach()


def main():
    if os.name!='nt':
        print('Windows Edge window smoke test: not run on this platform.')
        return
    engine=SimpleNamespace(interrupt=threading.Event(),shutdown=threading.Event(),started=False,recovering=False,
                           log=lambda _:None,guard_preparation=lambda:None)
    pool=SessionPool();owned=ManagedSession('window-smoke',engine,pool)
    control=None
    with TemporaryDirectory(prefix='pd-window-smoke-control-') as directory:
        try:
            page=owned._first_page();page.set_content('<html><body>Managed test window</body></html>')
            control=pool.get_runtime().chromium.launch_persistent_context(directory,channel='msedge',headless=False,chromium_sandbox=True)
            other=control.pages[0];other.set_content('<html><body>Independent test window</body></html>')
            before=window_state(control,other)
            owned.minimize_owned_window(page)
            assert window_state(owned._context,page)=='minimized', 'Assigned window did not minimize'
            assert window_state(control,other)==before, 'Independent window changed state'
            owned.restore_owned_window(page)
            assert window_state(owned._context,page)=='normal', 'Assigned window did not restore'
            print('PASS: only the assigned Edge window minimized and restored; no external navigation.')
        finally:
            if control is not None:control.close()
            owned.close();pool.close()

if __name__=='__main__':main()
