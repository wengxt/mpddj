xgettext -L Python main.py -o locale/pot/mpddj.pot
for f in locale/*/LC_MESSAGES/mpddj.po; do
    msgmerge -vU "$f" locale/pot/mpddj.pot
    msgfmt "$f" -o "${f/po/mo}"
done
