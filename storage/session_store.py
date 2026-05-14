"""
Session storage abstraction.

Текущая имплементация — на файлах (storage/session.json), уже реализована
в client.session.SessionManager. Этот модуль зарезервирован под Redis-вариант,
если понадобится несколько процессов / контейнеризация.
"""
from __future__ import annotations

# TODO: Redis-backed реализация после прототипа на файлах
