# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import logging
import pysftp
import tempfile
import os
import base64
from os.path import normpath
from Crypto import Random
from Crypto.Cipher import AES
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import StreamingHttpResponse
from django.core.servers.basehttp import FileWrapper
from django.core.urlresolvers import reverse
from django.shortcuts import render, redirect
from .models import Host

logger = logging.getLogger(__name__)


def create_session(hostname, username, password):
    return pysftp.Connection(hostname, username=username, password=password)

@login_required
def files_view(request):
    """The main filecenter view."""
    if not request.user.has_admin_permission('files'):
        return render(request, "files/devel_message.html")


    hosts = Host.objects.all()

    context = {
        "hosts": hosts
    }
    return render(request, "files/home.html", context)

@login_required
def files_auth(request):
    """Display authentication for filecenter."""
    if "password" in request.POST:
        key = Random.new().read(32)
        iv = Random.new().read(16)
        obj = AES.new(key, AES.MODE_CFB, iv)
        message = request.POST.get("password")
        ciphertext = obj.encrypt(message)
        request.session["files_iv"] = base64.b64encode(iv)
        request.session["files_text"] = base64.b64encode(ciphertext)
        cookie_key = base64.b64encode(key)

        nexturl = request.GET.get("next", None)
        if nexturl and nexturl.startswith("/files"):
            response = redirect(nexturl)
        else:
            response = redirect("files")
        response.set_cookie(key="files_key", value=cookie_key)
        return response
    else:
        return render(request, "files/auth.html", {})


def get_authinfo(request):
    if (("files_iv" not in request.session) or 
        ("files_text" not in request.session) or 
        ("files_key" not in request.COOKIES)):
        return False

    iv = base64.b64decode(request.session["files_iv"])
    text = base64.b64decode(request.session["files_text"])
    key = base64.b64decode(request.COOKIES["files_key"])

    obj = AES.new(key, AES.MODE_CFB, iv)
    password = obj.decrypt(text)

    return {
        "username": request.user.username,
        "password": password
    }

@login_required
def files_type(request, fstype=None):
    
    try:
        host = Host.objects.get(code=fstype)
    except Host.DoesNotExist:
        messages.error(request, "Could not find host in database.")
        return redirect("files")

    authinfo = get_authinfo(request)

    if not authinfo:
        return redirect("{}?next={}".format(reverse("files_auth"), request.get_full_path()))

    try:
        sftp = create_session(host.address, authinfo["username"], authinfo["password"])
    except pysftp.SSHException as e:
        messages.error(request, e)
        return redirect("files")

    if host.directory:
        host_dir = host.directory
        if "{}" in host_dir:
            host_dir = host_dir.format(authinfo["username"])
        sftp.chdir(host_dir)

    default_dir = sftp.pwd

    def can_access_path(fsdir):
        #if request.user.has_admin_permission('files'):
        #    return True
        return normpath(fsdir).startswith(default_dir)


    if "file" in request.GET:
        # Download file
        filepath = request.GET.get("file")
        filepath = normpath(filepath)
        filebase = os.path.basename(filepath)
        if can_access_path(filepath):
            tmpfile = tempfile.NamedTemporaryFile(prefix="ion_{}_{}".format(request.user.username, filebase))
            tmppath = tmpfile.name
            logger.debug(tmpfile)
            logger.debug(tmppath)
            sftp.get(filepath, localpath=tmppath)
            try:
                tmpopen = open(tmppath)
            except IOError:
                messages.error(request, "Unable to download {}".format(filebase))
                return redirect("/files/{}/?dir={}".format(fstype, os.path.dirname(filepath)))
            else:
                chunk_size = 8192
                response = StreamingHttpResponse(FileWrapper(tmpopen, chunk_size), content_type="application/octet-stream")
                response['Content-Length'] = os.path.getsize(tmppath)
                response["Content-Disposition"] = "attachment; filename={}".format(filebase)
                return response

    fsdir = request.GET.get("dir")
    if fsdir:
        fsdir = normpath(fsdir)
        if can_access_path(fsdir):
            sftp.chdir(fsdir)
        else:
            messages.error(request, "Access to the path you provided is restricted.")
            return redirect("/files/{}/?dir={}".format(fstype, default_dir))

    try:
        listdir = sftp.listdir()
    except IOError as e:
        messages.error(request, e)
        listdir = []
    files = []
    for f in listdir:
        if not f.startswith("."):
            files.append({
                "name": f,
                "folder": sftp.isdir(f),
            })

    current_dir = sftp.pwd # current directory
    dir_list = current_dir.split("/")
    if len(dir_list) > 1 and len(dir_list[-1]) == 0:
        dir_list.pop()
    parent_dir = "/".join(dir_list[:-1])

    if len(parent_dir) == 0:
        parent_dir = "/"

    context = {
        "host": host,
        "files": files,
        "current_dir": current_dir,
        "parent_dir": parent_dir if can_access_path(parent_dir) else None
    }

    return render(request, "files/directory.html", context)

