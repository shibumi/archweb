import datetime
import json
from string import Template
from urllib import urlencode

from django.http import HttpResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.timezone import now
from django.views.generic.simple import direct_to_template

from main.models import Package, PackageFile, Arch, Repo
from mirrors.utils import get_mirror_url_for_download
from ..models import Update
from ..utils import get_group_info, PackageJSONEncoder


def split_package_details(request, name, repo, arch):
    arch = get_object_or_404(Arch, name=arch)
    arches = [ arch ]
    arches.extend(Arch.objects.filter(agnostic=True))
    repo = get_object_or_404(Repo, name__iexact=repo)
    pkgs = Package.objects.normal().filter(pkgbase=name,
            repo__testing=repo.testing, repo__staging=repo.staging,
            arch__in=arches).order_by('pkgname')
    if len(pkgs) == 0:
        return None
    # we have packages, but ensure at least one is in the given repo
    if not any(True for pkg in pkgs if pkg.repo == repo):
        return None
    context = {
        'list_title': 'Split Package Details',
        'name': name,
        'arch': arch,
        'packages': pkgs,
    }
    return direct_to_template(request, 'packages/packages_list.html',
            context)


CUTOFF = datetime.timedelta(days=60)


def recently_removed_package(request, name, repo, arch, cutoff=CUTOFF):
    '''We're just steps away from raising a 404, but check our packages update
    table first to see if this package has existed in this repo before. If so,
    we can show a 410 Gone page and point the requester in the right
    direction.'''
    arch = get_object_or_404(Arch, name=arch)
    arches = [ arch ]
    arches.extend(Arch.objects.filter(agnostic=True))
    match = Update.objects.select_related('arch', 'repo').filter(
            pkgname=name, repo__name__iexact=repo, arch__in=arches)
    if cutoff is not None:
        when = now() - cutoff
        match = match.filter(created__gte=when)
    try:
        match = match.latest()
        return render(request, 'packages/removed.html',
                {'update': match, }, status=410)
    except Update.DoesNotExist:
        return None


def details(request, name='', repo='', arch=''):
    if all([name, repo, arch]):
        try:
            pkg = Package.objects.select_related(
                    'arch', 'repo', 'packager').get(pkgname=name,
                    repo__name__iexact=repo, arch__name=arch)
            return direct_to_template(request, 'packages/details.html',
                    {'pkg': pkg, })
        except Package.DoesNotExist:
            arch_obj = get_object_or_404(Arch, name=arch)
            # for arch='any' packages, we can issue a redirect to them if we
            # have a single non-ambiguous option by changing the arch to match
            # any arch-agnostic package
            if not arch_obj.agnostic:
                pkgs = Package.objects.select_related(
                    'arch', 'repo', 'packager').filter(pkgname=name,
                    repo__name__iexact=repo, arch__agnostic=True)
                if len(pkgs) == 1:
                    return redirect(pkgs[0], permanent=True)
            # do we have a split package matching this criteria?
            ret = split_package_details(request, name, repo, arch)
            if ret is None:
                # maybe we have a recently-removed package?
                ret = recently_removed_package(request, name, repo, arch)
            if ret is not None:
                return ret
            else:
                # we've tried everything at this point, nothing to see
                raise Http404
    else:
        pkg_data = [
            ('arch', arch.lower()),
            ('repo', repo.lower()),
            ('q',    name),
        ]
        # only include non-blank values in the query we generate
        pkg_data = [(x, y.encode('utf-8')) for x, y in pkg_data if y]
        return redirect("/packages/?%s" % urlencode(pkg_data))


def groups(request, arch=None):
    arches = []
    if arch:
        get_object_or_404(Arch, name=arch, agnostic=False)
        arches.append(arch)
    grps = get_group_info(arches)
    context = {
        'groups': grps,
        'arch': arch,
    }
    return direct_to_template(request, 'packages/groups.html', context)


def group_details(request, arch, name):
    arch = get_object_or_404(Arch, name=arch)
    arches = [ arch ]
    arches.extend(Arch.objects.filter(agnostic=True))
    pkgs = Package.objects.normal().filter(
            groups__name=name, arch__in=arches).order_by('pkgname')
    if len(pkgs) == 0:
        raise Http404
    context = {
        'list_title': 'Group Details',
        'name': name,
        'arch': arch,
        'packages': pkgs,
    }
    return direct_to_template(request, 'packages/packages_list.html', context)


def files(request, name, repo, arch):
    pkg = get_object_or_404(Package,
            pkgname=name, repo__name__iexact=repo, arch__name=arch)
    # files are inserted in sorted order, so preserve that
    fileslist = PackageFile.objects.filter(pkg=pkg).order_by('id')
    dir_count = sum(1 for f in fileslist if f.is_directory)
    files_count = len(fileslist) - dir_count
    context = {
        'pkg': pkg,
        'files': fileslist,
        'files_count': files_count,
        'dir_count': dir_count,
    }
    template = 'packages/files.html'
    return direct_to_template(request, template, context)


def details_json(request, name, repo, arch):
    pkg = get_object_or_404(Package,
            pkgname=name, repo__name__iexact=repo, arch__name=arch)
    to_json = json.dumps(pkg, ensure_ascii=False, cls=PackageJSONEncoder)
    return HttpResponse(to_json, mimetype='application/json')


def files_json(request, name, repo, arch):
    pkg = get_object_or_404(Package,
            pkgname=name, repo__name__iexact=repo, arch__name=arch)
    # files are inserted in sorted order, so preserve that
    fileslist = PackageFile.objects.filter(pkg=pkg).order_by('id')
    data = {
        'pkgname': pkg.pkgname,
        'repo': pkg.repo.name.lower(),
        'arch': pkg.arch.name.lower(),
        'files': fileslist,
    }
    to_json = json.dumps(data, ensure_ascii=False, cls=PackageJSONEncoder)
    return HttpResponse(to_json, mimetype='application/json')


def download(request, name, repo, arch):
    pkg = get_object_or_404(Package,
            pkgname=name, repo__name__iexact=repo, arch__name=arch)
    url = get_mirror_url_for_download()
    if not url:
        raise Http404
    arch = pkg.arch.name
    if pkg.arch.agnostic:
        # grab the first non-any arch to fake the download path
        arch = Arch.objects.exclude(agnostic=True)[0].name
    values = {
        'host': url.url,
        'arch': arch,
        'repo': pkg.repo.name.lower(),
        'file': pkg.filename,
    }
    url = Template('${host}${repo}/os/${arch}/${file}').substitute(values)
    return redirect(url)

# vim: set ts=4 sw=4 et:
