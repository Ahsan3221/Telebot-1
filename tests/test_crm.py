import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from crm import (parse_game,parse_source,normalize_game,normalize_label,
                 normalize_workflow,safe_username,topic_title,profile_text,
                 retry_delay,is_permanent_error,is_topic_missing)


def test_game_case_insensitive():
    assert parse_game(['juWa'])=='Juwa'
    assert parse_game(['vblink'])=='Vblink'
    assert parse_game(['wrong']) is None


def test_source_links():
    assert parse_source(['src_facebook'])=='📘 Facebook'
    assert parse_source(['website'])=='🌐 Website'
    assert parse_source(['juwa']) is None


def test_username_validation():
    assert safe_username('@player_123')=='player_123'
    for username in ('','x'*90,'a b','some/password','name\nmore'):
        with pytest.raises(ValueError):safe_username(username)


def test_game_normalization():
    assert normalize_game('OrionStars')=='Orionstars'
    with pytest.raises(ValueError):normalize_game('fakegame')


def test_labels_and_workflow():
    assert normalize_label('VIP')=='vip'
    assert normalize_workflow('in-progress')=='in_progress'
    with pytest.raises(ValueError):normalize_label('banned')
    with pytest.raises(ValueError):normalize_workflow('blocked')


def test_topic_title():
    title=topic_title({'user_id':100,'name':'Sarah\nJones','primary_label':'vip','game':'Juwa'})
    assert title.startswith('👑 VIP | Sarah Jones')
    assert '| Juwa' in title
    assert '\n' not in title


def test_profile_multiple_accounts():
    row={'user_id':123,'name':'Alice','username':'alice','game':'Juwa',
         'primary_label':'vip','workflow_status':'pending','assigned_staff':42,'bonus':None,'source':None}
    accounts=[{'game':'Juwa','account_username':'alice123'},
              {'game':'Orionstars','account_username':'alice456'}]
    text=profile_text(row,accounts,['vip','depositor'],[{'body':'Helpful customer'}],{'count':2,'amount':75})
    assert 'alice123' in text and 'alice456' in text
    assert '👑 VIP' in text and '💰 Depositor' in text
    assert 'Recorded deposits: 2 | $75.00' in text


def test_retry_backoff():
    assert retry_delay(1)==2
    assert retry_delay(5)==32
    assert retry_delay(200)==1024


def test_permanent_delivery_errors():
    assert is_permanent_error(Exception('Forbidden: bot was blocked by the user'))
    assert not is_permanent_error(Exception('Telegram temporarily unavailable'))


def test_topic_error_recognition():
    assert is_topic_missing(Exception('Bad Request: message thread not found'))
    assert is_topic_missing(Exception('topic_closed'))
    assert not is_topic_missing(Exception('Too Many Requests: retry after 3'))
