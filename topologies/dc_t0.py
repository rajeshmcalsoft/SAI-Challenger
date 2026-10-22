from contextlib import contextmanager


@contextmanager
def config(npu):
    topo_cfg = {

    }
    
    # SETUP
    yield topo_cfg
    # TEARDOWN

