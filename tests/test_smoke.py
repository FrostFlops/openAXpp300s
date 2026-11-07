def test_status(cli):
    code, docs = cli('status')
    assert code == 0 and docs[-1]['ok'] and docs[-1]['port'] == 'SIM'


def test_set_on_off(cli):
    code, docs = cli('set', '--voltage', '5', '--current', '1', '--ovp', '6')
    assert code == 0, docs
    code, docs = cli('on')
    assert code == 0 and docs[-1]['settled'], docs
    assert abs(docs[-1]['status']['vout_v'] - 5.0) < 0.01
    code, docs = cli('off')
    assert code == 0 and not docs[-1]['status']['output_on']
