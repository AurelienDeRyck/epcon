# -*- coding: UTF-8 -*-
from django.conf import settings as dsettings
from django.core.cache import cache
from django.core.mail import send_mail as real_send_mail

from conference import settings
from conference.models import VotoTalk

import json
import logging
import os.path
import re
import subprocess
import tempfile
import urllib2
from collections import defaultdict

log = logging.getLogger('conference')

def dotted_import(path):
    from django.utils.importlib import import_module
    from django.core.exceptions import ImproperlyConfigured
    i = path.rfind('.')
    module, attr = path[:i], path[i+1:]

    try:
        mod = import_module(module)
    except ImportError, e:
        raise ImproperlyConfigured('Error importing %s: "%s"' % (path, e))

    try:
        o = getattr(mod, attr)
    except AttributeError:
        raise ImproperlyConfigured('Module "%s" does not define "%s"' % (module, attr))

    return o

def send_email(force=False, *args, **kwargs):
    if force is False and not settings.SEND_EMAIL_TO:
        return
    if 'recipient_list' not in kwargs:
        kwargs['recipient_list'] = settings.SEND_EMAIL_TO
    if 'from_email' not in kwargs:
        kwargs['from_email'] = dsettings.DEFAULT_FROM_EMAIL
    real_send_mail(*args, **kwargs)

def _input_for_ranking_of_talks(talks, missing_vote=5):
    """
    Dato un elenco di talk restituisce l'input da passare a vengine; se un
    utente non ha espresso una preferenza per un talk gli viene assegnato il
    valore `missing_vote`.
    """
    # se talks è un QuerySet evito di interrogare il db più volte
    talks = list(talks)
    tids = set(t.id for t in talks)
    # come candidati uso gli id dei talk...
    cands = ' '.join(map(str, tids))
    # e nel caso di pareggi faccio vincere il talk presentato prima
    tie = ' '.join(str(t.id) for t in sorted(talks, key=lambda x: x.created))
    vinput = [
        '-m schulze',
        '-cands %s -tie %s' % (cands, tie),
    ]
    for t in talks:
        vinput.append('# %s - %s' % (t.id, t.title.encode('utf-8')))

    votes = VotoTalk.objects\
        .filter(talk__in=tids)\
        .order_by('user', '-vote')
    users = defaultdict(lambda: defaultdict(list))
    for vote in votes:
        users[vote.user_id][vote.vote].append(vote.talk_id)

    for votes in users.values():
        # tutti i talk non votati dall'utente ottengono il voto standard
        # `missing_vote`
        missing = tids - set(sum(votes.values(), []))
        if missing:
            votes[missing_vote].extend(missing)

        # per esprimere le preferenze nel formati di vengin:
        #   cand1=cand2 -> i due candidati hanno avuto la stessa preferenza
        #   cand1>cand2 -> cand1 ha avuto più preferenze di cand2
        #   cand1=cand2>cand3 -> cand1 uguale a cand2 entrambi maggiori di cand3
        input_line = []
        ballot = sorted(votes.items(), reverse=True)
        for vote, tid in ballot:
            input_line.append('='.join(map(str, tid)))
        vinput.append('>'.join(input_line))

    return '\n'.join(vinput)

def ranking_of_talks(talks, missing_vote=5):
    import conference
    vengine = os.path.join(os.path.dirname(conference.__file__), 'utils', 'voteengine-0.99', 'voteengine.py')

    talks_map = dict((t.id, t) for t in talks)
    in_ = _input_for_ranking_of_talks(talks_map.values(), missing_vote=missing_vote)

    pipe = subprocess.Popen(
        [vengine],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True
    )
    out, err = pipe.communicate(in_)

    return [ talks_map[int(tid)] for tid in re.findall(r'\d+', out.split('\n')[-2]) ]

def latest_tweets(screen_name, count):
    import twitter
    key = 'conf:latest:tweets:%s:%s' % (screen_name, count)
    data = cache.get(key)
    if data is None:
        client = twitter.Api()
        # il modulo twitter fornisce un meccanismo di caching, ma
        # preferisco disabilitarlo per due motivi:
        #
        # 1. voglio usare la cache di django, in questo modo modificando i
        # settings modifico anche la cache per twitter
        # 2. di default, ma è modificabile con le api pubbliche, la cache
        # del modulo twitter è file-based e sbaglia a decidere la directory
        # in cui salvare i file; finisce per scrivere in
        # /tmp/python.cache_nobody/... che non è detto sia scrivibile
        # (sopratutto se la directory è stata creata da un altro processo
        # che gira con un utente diverso).
        client.SetCache(None)
        try:
            tweets = client.GetUserTimeline(screen_name, count=count)
        except (ValueError, urllib2.HTTPError):
            # ValueError: a volte twitter.com non risponde correttamente, e
            # twitter (il modulo) non verifica. Di conseguenza viene
            # sollevato un ValueError quando twitter (il modulo) tenta
            # parsare un None come se fosse una stringa json.

            # HTTPError: a volte twitter.com non risponde proprio :) (in
            # realtà risponde con un 503)

            # Spesso questi errori durano molto poco, ma per essere gentile
            # con twitter cacho un risultato nullo per poco tempo.
            tweets = []
        except:
            # vista la stabilità di twitter meglio cachare tutto per
            # evitare errori sul nostro sito
            tweets = []
        data = [{
                    "text": tweet.GetText(),
                    "timestamp": tweet.GetCreatedAtInSeconds(),
                    "followers_count": tweet.user.GetFollowersCount(),
                    "id": tweet.GetId(),
                } for tweet in tweets]
        cache.set(key, data, 60 * 5)
    return data

from datetime import datetime, date, timedelta

class TimeTable(object):
    class Event(object):
        def __init__(self, time, row, ref, columns, rows):
            self.time = time
            self.row = row
            self.ref = ref
            self.columns = columns
            self.rows = rows

    class Reference(object):
        def __init__(self, time, row, evt, flex=False):
            self.time = time
            self.row = row
            self.evt = evt
            self.flex = flex

    def __init__(self, time_spans, rows, slot_length=15):
        self.start, self.end = time_spans
        assert self.start < self.end
        self.rows = rows
        assert self.rows
        self.slot = timedelta(seconds=slot_length*60)

        self._data = {}
        self.errors = []

    def slice(self, start=None, end=None):
        if not start:
            start = self.start
        if not end:
            end = self.end
        if end < start:
            start, end = end, start
        t2 = TimeTable(
            (start, end),
            self.rows,
            self.slot.seconds/60,
        )
        t2._data = dict(self._data)
        t2.errors = list(self.errors)

        for key in list(t2._data):
            if (start and key[0] < start) or (end and key[0] >= end):
                del t2._data[key]

        return t2

    @classmethod
    def sumTime(cls, t, td):
        return ((datetime.combine(date.today(), t)) + td).time()

    @classmethod
    def diffTime(cls, t1, t2):
        return ((datetime.combine(date.today(), t1)) - (datetime.combine(date.today(), t2)))

    def setEvent(self, time, o, duration, rows):
        assert rows
        assert not (set(rows) - set(self.rows))
        if not duration:
            next = self.findFirstEvent(self.sumTime(time, self.slot), rows[0])
            if not next:
                duration = self.diffTime(self.end, time).seconds / 60
            else:
                duration = self.diffTime(next.time, time).seconds / 60
            flex = True
        else:
            flex = False
        count = duration / (self.slot.seconds / 60)

        evt = TimeTable.Event(time, rows[0], o, count, len(rows))
        self._setEvent(evt, flex)
        for r in rows[1:]:
            ref = TimeTable.Reference(time, r, evt, flex)
            self._setEvent(ref, flex)
            
        step = self.sumTime(time, self.slot)
        while count > 1:
            for r in rows:
                ref = TimeTable.Reference(step, r, evt, flex)
                self._setEvent(ref, flex)
            step = self.sumTime(step, self.slot)
            count -= 1

    def _setEvent(self, evt, flex):
        event = evt.ref if isinstance(evt, TimeTable.Event) else evt.evt.ref
        try:
            prev = self._data[(evt.time, evt.row)]
        except KeyError:
            pass
        else:
            if isinstance(prev, TimeTable.Event):
                self.errors.append({
                    'type': 'overlap-event',
                    'time': evt.time,
                    'event': event,
                    'previous': prev.ref,
                    'msg': 'Event %s overlap %s on time %s' % (event, prev.ref, evt.time),
                })
                return
            elif isinstance(prev, TimeTable.Reference):
                # sto cercando di inserire un evento su una cella occupata da
                # un reference, un estensione temporale di un altro evento. 
                #
                # Se nessuno dei due eventi (il nuovo e il precedente) è flex
                # notifico un warning (nel caso uno dei due sia flex non dico
                # nulla perché gli eventi flessibili sono nati proprio per
                # adattarsi agli altri); dopodiché accorcio, se possibile,
                # l'evento precedente.
                if not prev.flex and not flex:
                    self.errors.append({
                        'type': 'overlap-reference',
                        'time': evt.time,
                        'event': event,
                        'previous': prev.evt.ref,
                        'msg': 'Event %s overlap %s on time %s' % (event, prev.evt.ref, evt.time),
                    })

                # accorcio l'evento precedente solo se è di tipo flex oppure se
                # l'evento che sto inserendo non *è* flex. Questo copre il caso
                # in cui un talk va a posizionarsi in una cella occupata da un
                # estensione di un pranzo (evento flex) oppure quando un
                # evento flex a sua volta va a coprire un estensione di un
                # altro evento flex (ad esempio due eventi custom uno dopo
                # l'altro).
                if not flex or prev.flex:
                    evt0 = prev.evt
                    columns = self.diffTime(evt.time, evt0.time).seconds / self.slot.seconds
                    for row in self.rows:
                        for e in self.iterOnRow(row, start=evt.time):
                            if isinstance(e, TimeTable.Reference) and e.evt is evt0:
                                del self._data[(e.time, e.row)]
                            else:
                                break
                    evt0.columns = columns
        self._data[(evt.time, evt.row)] = evt

    def iterOnRow(self, row, start=None, end=None):
        if start is None:
            start = self.start
        if end is None:
            end = self.end
        while start < end:
            try:
                yield self._data[(start, row)]
            except KeyError:
                pass
            start = self.sumTime(start, self.slot)

    def findFirstEvent(self, start, row):
        for evt in self.iterOnRow(row, start=start):
            if isinstance(evt, TimeTable.Reference) and evt.evt.time >= start:
                return evt.evt
            elif isinstance(evt, TimeTable.Event):
                return evt

    def columns(self):
        step = self.start
        while step < self.end:
            yield step
            step = self.sumTime(step, self.slot)

    def eventsAtTime(self, start, include_reference=False):
        """
        ritorna le righe che contengono un Event (e un Reference se
        include_reference=True) al tempo passato.
        """
        output = []
        for r in self.rows:
            try:
                cell = self._data[(start, r)]
            except KeyError:
                continue
            if include_reference or isinstance(cell, TimeTable.Event):
                output.append(cell)
        return output

    def changesAtTime(self, start):
        """
        ritorna le righe che introducono un cambiamento al tempo passato.
        """
        output = []
        for key, item in self._data.items():
            if not isinstance(item, TimeTable.Event):
                continue
            if key[0] == start or self.sumTime(key[0], timedelta(seconds=self.slot.seconds*item.columns)) == start:
                output.append(key)
        return output

    def byRows(self):
        output = []
        data = self._data
        for row in self.rows:
            step = self.start

            cols = []
            line = [ row, cols ]
            while step < self.end:
                cols.append({'time': step, 'row': row, 'data': data.get((step, row))})
                step = self.sumTime(step, self.slot)

            output.append(line)

        return output

    def byTimes(self):
        output = []
        data = self._data
        #if not data:
        #    return output
        step = self.start
        while step < self.end:
            rows = []
            line = [ step, rows ]
            for row in self.rows:
                rows.append({'row': row, 'data': data.get((step, row))})
            output.append(line)
            step = self.sumTime(step, self.slot)

        return output

def render_badge(tickets, cmdargs=None):
    if cmdargs is None:
        cmdargs = []
    files = []
    for group in settings.TICKET_BADGE_PREPARE_FUNCTION(tickets):
        tfile = tempfile.NamedTemporaryFile(suffix='.tar')
        args = [settings.TICKED_BADGE_PROG, '-o', tfile.name] + cmdargs + list(group['args'])
        p = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        sout, serr = p.communicate(json.dumps(group['tickets']))
        if p.returncode:
            log.warn('badge maker exit with "%s"', p.returncode)
            log.warn('badge maker stderr: %s', serr)
        tfile.seek(0)
        files.append(tfile)
    return files
