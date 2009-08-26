""" This module is used to build a "Package", a collection of files
within a Panda3D Multifile, which can be easily be downloaded and/or
patched onto a client machine, for the purpose of running a large
application. """

import sys
import os
import glob
import marshal
import new
import string
import types
from direct.p3d.FileSpec import FileSpec
from direct.showbase import Loader
from direct.showbase import AppRunnerGlobal
from direct.showutil import FreezeTool
from direct.directnotify.DirectNotifyGlobal import *
from pandac.PandaModules import *

vfs = VirtualFileSystem.getGlobalPtr()

class PackagerError(StandardError):
    pass

class OutsideOfPackageError(PackagerError):
    pass

class ArgumentError(PackagerError):
    pass

class Packager:
    notify = directNotify.newCategory("Packager")

    class PackFile:
        def __init__(self, package, filename,
                     newName = None, deleteTemp = False,
                     explicit = False, compress = None, extract = None,
                     text = None, unprocessed = None,
                     executable = None, platformSpecific = None):
            assert isinstance(filename, Filename)
            self.filename = Filename(filename)
            self.newName = newName
            self.deleteTemp = deleteTemp
            self.explicit = explicit
            self.compress = compress
            self.extract = extract
            self.text = text
            self.unprocessed = unprocessed
            self.executable = executable
            self.platformSpecific = platformSpecific

            if not self.newName:
                self.newName = self.filename.cStr()

            ext = Filename(self.newName).getExtension()
            if ext == 'pz':
                # Strip off a .pz extension; we can compress files
                # within the Multifile without it.
                filename = Filename(self.newName)
                filename.setExtension('')
                self.newName = filename.cStr()
                ext = Filename(self.newName).getExtension()
                if self.compress is None:
                    self.compress = True

            packager = package.packager
            if self.compress is None:
                self.compress = (ext not in packager.uncompressibleExtensions and ext not in packager.imageExtensions)

            if self.executable is None:
                self.executable = (ext in packager.executableExtensions)

            if self.extract is None:
                self.extract = self.executable or (ext in packager.extractExtensions)
            if self.platformSpecific is None:
                self.platformSpecific = self.executable or (ext in packager.platformSpecificExtensions)
                
            if self.unprocessed is None:
                self.unprocessed = self.executable or (ext in packager.unprocessedExtensions)

            if self.executable:
                # Look up the filename along the system PATH, if necessary.
                self.filename.resolveFilename(packager.executablePath)

            # Convert the filename to an unambiguous filename for
            # searching.
            self.filename.makeTrueCase()
            if self.filename.exists() or not self.filename.isLocal():
                self.filename.makeCanonical()

        def isExcluded(self, package):
            """ Returns true if this file should be excluded or
            skipped, false otherwise. """

            if self.newName in package.skipFilenames:
                return True

            if not self.explicit:
                # Make sure it's not one of our auto-excluded system
                # files.  (But only make this check if this file was
                # not explicitly added.)

                basename = Filename(self.newName).getBasename()
                if not package.packager.caseSensitive:
                    basename = basename.lower()
                if basename in package.packager.excludeSystemFiles:
                    return True
                for exclude in package.packager.excludeSystemGlobs:
                    if exclude.matches(basename):
                        return True

                # Also check if it was explicitly excluded.  As above,
                # omit this check for an explicitly-added file: if you
                # both include and exclude a file, the file is
                # included.
                for exclude in package.excludedFilenames:
                    if exclude.matches(self.filename):
                        return True

                # A platform-specific file is implicitly excluded from
                # not-platform-specific packages.
                if self.platformSpecific and package.platformSpecificConfig is False:
                    return True

            return False
                
    class ExcludeFilename:
        def __init__(self, filename, caseSensitive):
            self.localOnly = (not filename.get_dirname())
            if not self.localOnly:
                filename = Filename(filename)
                filename.makeCanonical()
            self.glob = GlobPattern(filename.cStr())

            if PandaSystem.getPlatform().startswith('win'):
                self.glob.setCaseSensitive(False)
            elif PandaSystem.getPlatform().startswith('osx'):
                self.glob.setCaseSensitive(False)

        def matches(self, filename):
            if self.localOnly:
                return self.glob.matches(filename.getBasename())
            else:
                return self.glob.matches(filename.cStr())

    class PackageEntry:
        """ This corresponds to a <package> entry in the contents.xml
        file. """
        
        def __init__(self):
            pass

        def getKey(self):
            """ Returns a tuple used for sorting the PackageEntry
            objects uniquely per package. """
            return (self.packageName, self.platform, self.version)

        def fromFile(self, packageName, platform, version, solo,
                     installDir, descFilename, importDescFilename):
            self.packageName = packageName
            self.platform = platform
            self.version = version
            self.solo = solo

            self.descFile = FileSpec()
            self.descFile.fromFile(installDir, descFilename)

            self.importDescFile = None
            if importDescFilename:
                self.importDescFile = FileSpec()
                self.importDescFile.fromFile(installDir, importDescFilename)

        def loadXml(self, xpackage):
            self.packageName = xpackage.Attribute('name')
            self.platform = xpackage.Attribute('platform')
            self.version = xpackage.Attribute('version')
            solo = xpackage.Attribute('solo')
            self.solo = int(solo or '0')

            self.descFile = FileSpec()
            self.descFile.loadXml(xpackage)

            self.importDescFile = None
            ximport = xpackage.FirstChildElement('import')
            if ximport:
                self.importDescFile = FileSpec()
                self.importDescFile.loadXml(ximport)
            

        def makeXml(self):
            """ Returns a new TiXmlElement. """
            xpackage = TiXmlElement('package')
            xpackage.SetAttribute('name', self.packageName)
            if self.platform:
                xpackage.SetAttribute('platform', self.platform)
            if self.version:
                xpackage.SetAttribute('version', self.version)
            if self.solo:
                xpackage.SetAttribute('solo', '1')

            self.descFile.storeXml(xpackage)

            if self.importDescFile:
                ximport = TiXmlElement('import')
                self.importDescFile.storeXml(ximport)
                xpackage.InsertEndChild(ximport)
            
            return xpackage

    class Package:
        """ This is the full information on a particular package we
        are constructing.  Don't confuse it with PackageEntry, above,
        which contains only the information found in the toplevel
        contents.xml file."""
        
        def __init__(self, packageName, packager):
            self.packageName = packageName
            self.packager = packager
            self.notify = packager.notify
            
            self.platform = None
            self.version = None
            self.host = None
            self.p3dApplication = False
            self.solo = False
            self.compressionLevel = 0
            self.importedMapsDir = 'imported_maps'
            self.mainModule = None
            self.requires = []

            # This is the set of config variables assigned to the
            # package.
            self.configs = {}

            # This is the set of files and modules, already included
            # by required packages, that we can skip.
            self.skipFilenames = {}
            self.skipModules = {}

            # This is a list of ExcludeFilename objects, representing
            # the files that have been explicitly excluded.
            self.excludedFilenames = []

            # This is the list of files we will be adding, and a pair
            # of cross references.
            self.files = []
            self.sourceFilenames = {}
            self.targetFilenames = {}

            # This records the current list of modules we have added so
            # far.
            self.freezer = FreezeTool.Freezer()

        def close(self):
            """ Writes out the contents of the current package. """

            if not self.host:
                self.host = self.packager.host

            # Check the version config variable.
            version = self.configs.get('version', None)
            if version is not None:
                self.version = version
                del self.configs['version']

            # Check the platform_specific config variable.  This has
            # only three settings: None (unset), True, or False.
            self.platformSpecificConfig = self.configs.get('platform_specific', None)
            if self.platformSpecificConfig is not None:
                self.platformSpecificConfig = bool(self.platformSpecificConfig)
                del self.configs['platform_specific']

            # A special case when building the "panda3d" package.  We
            # enforce that the version number matches what we've been
            # compiled with.
            if self.packageName == 'panda3d':
                if self.version is None:
                    self.version = PandaSystem.getPackageVersionString()

                if self.version != PandaSystem.getPackageVersionString():
                    message = 'mismatched Panda3D version: requested %s, but Panda3D is built as %s' % (self.version, PandaSystem.getPackageVersionString())
                    raise PackagerError, message

                if self.host != PandaSystem.getPackageHostUrl():
                    message = 'mismatched Panda3D host: requested %s, but Panda3D is built as %s' % (self.host, PandaSystem.getPackageHostUrl())
                    raise PackagerError, message

            if self.p3dApplication:
                # Default compression level for an app.
                self.compressionLevel = 6

                # Every p3dapp requires panda3d.
                self.packager.do_require('panda3d')
            
            if not self.p3dApplication and not self.version:
                # If we don't have an implicit version, inherit the
                # version from the 'panda3d' package on our require
                # list.
                for p2 in self.requires:
                    if p2.packageName == 'panda3d' and p2.version:
                        self.version = p2.version
                        break

            if self.solo:
                self.installSolo()
            else:
                self.installMultifile()

        def considerPlatform(self):
            # Check to see if any of the files are platform-specific,
            # making the overall package platform-specific.

            platformSpecific = self.platformSpecificConfig
            for file in self.files:
                if file.isExcluded(self):
                    # Skip this file.
                    continue
                if file.platformSpecific:
                    platformSpecific = True

            if platformSpecific and self.platformSpecificConfig is not False:
                if not self.platform:
                    self.platform = PandaSystem.getPlatform()
            

        def installMultifile(self):
            """ Installs the package, either as a p3d application, or
            as a true package.  Either is implemented with a
            Multifile. """
            
            self.multifile = Multifile()

            if self.p3dApplication:
                self.multifile.setHeaderPrefix('#! /usr/bin/env panda3d\n')

            # Write the multifile to a temporary filename until we
            # know enough to determine the output filename.
            multifileFilename = Filename.temporary('', self.packageName + '.', '.mf')
            self.multifile.openReadWrite(multifileFilename)

            self.extracts = []
            self.components = []

            # Add the explicit py files that were requested by the
            # pdef file.  These get turned into Python modules.
            for file in self.files:
                if file.isExcluded(self):
                    # Skip this file.
                    continue
                if file.unprocessed:
                    # Unprocessed files get dealt with below.
                    continue

                ext = Filename(file.newName).getExtension()
                if ext == 'dc':
                    # Add the modules named implicitly in the dc file.
                    self.addDcImports(file)

                elif ext == 'py':
                    self.addPyFile(file)

            # Add the main module, if any.
            if not self.mainModule and self.p3dApplication:
                message = 'No main_module specified for application %s' % (self.packageName)
                raise PackagerError, message
            if self.mainModule:
                moduleName, newName = self.mainModule
                if newName not in self.freezer.modules:
                    self.freezer.addModule(moduleName, newName = newName)

            # Now all module files have been added.  Exclude modules
            # already imported in a required package, and not
            # explicitly included by this package.
            for moduleName, mdef in self.skipModules.items():
                if moduleName not in self.freezer.modules:
                    self.freezer.excludeModule(
                        moduleName, allowChildren = mdef.allowChildren,
                        forbid = mdef.forbid, fromSource = 'skip')

            # Pick up any unfrozen Python files.
            self.freezer.done()
            self.freezer.addToMultifile(self.multifile, self.compressionLevel)
            self.addExtensionModules()

            # Add known module names.
            self.moduleNames = {}
            modules = self.freezer.modules.items()
            modules.sort()
            for newName, mdef in modules:
                if mdef.guess:
                    # Not really a module.
                    continue

                if mdef.fromSource == 'skip':
                    # This record already appeared in a required
                    # module; don't repeat it now.
                    continue

                if mdef.exclude and mdef.implicit:
                    # Don't bother mentioning implicity-excluded
                    # (i.e. missing) modules.
                    continue

                if newName == '__main__':
                    # Ignore this special case.
                    continue

                self.moduleNames[newName] = mdef

                xmodule = TiXmlElement('module')
                xmodule.SetAttribute('name', newName)
                if mdef.exclude:
                    xmodule.SetAttribute('exclude', '1')
                if mdef.forbid:
                    xmodule.SetAttribute('forbid', '1')
                if mdef.exclude and mdef.allowChildren:
                    xmodule.SetAttribute('allowChildren', '1')
                self.components.append(('m', newName.lower(), xmodule))

            # Now look for implicit shared-library dependencies.
            if PandaSystem.getPlatform().startswith('win'):
                self.__addImplicitDependenciesWindows()
            elif PandaSystem.getPlatform().startswith('osx'):
                self.__addImplicitDependenciesOSX()
            else:
                self.__addImplicitDependenciesPosix()

            # Now add all the real, non-Python files (except model
            # files).  This will include the extension modules we just
            # discovered above.
            for file in self.files:
                ext = Filename(file.newName).getExtension()
                if file.unprocessed:
                    # Add an unprocessed file verbatim.
                    self.addComponent(file)
                elif ext == 'py':
                    # Already handled, above.
                    pass
                elif file.isExcluded(self):
                    # Skip this file.
                    pass
                elif ext == 'egg' or ext == 'bam':
                    # Skip model files this pass.
                    pass
                elif ext == 'dc':
                    # dc files get a special treatment.
                    self.addDcFile(file)
                elif ext == 'prc':
                    # So do prc files.
                    self.addPrcFile(file)
                else:
                    # Any other file.
                    self.addComponent(file)

            # Finally, now add the model files.  It's important to add
            # these after we have added all of the texture files, so
            # we can determine which textures need to be implicitly
            # pulled in.

            # We walk through the list as we modify it.  That's OK,
            # because we may add new files that we want to process.
            for file in self.files:
                ext = Filename(file.newName).getExtension()
                if file.unprocessed:
                    # Already handled, above.
                    pass
                elif ext == 'py':
                    # Already handled, above.
                    pass
                elif file.isExcluded(self):
                    # Skip this file.
                    pass
                elif ext == 'egg':
                    self.addEggFile(file)
                elif ext == 'bam':
                    self.addBamFile(file)
                else:
                    # Handled above.
                    pass

            # Check to see if we should be platform-specific.
            self.considerPlatform()

            # Now that we've processed all of the component files,
            # (and set our platform if necessary), we can generate the
            # output filename and write the output files.

            self.packageBasename = self.packageName
            packageDir = self.packageName
            if self.version:
                self.packageBasename += '.' + self.version
                packageDir += '/' + self.version
            if self.platform:
                self.packageBasename += '.' + self.platform
                packageDir += '/' + self.platform

            self.packageDesc = self.packageBasename + '.xml'
            self.packageImportDesc = self.packageBasename + '.import.xml'
            if self.p3dApplication:
                self.packageBasename += '.p3d'
                packageDir = ''
            else:
                self.packageBasename += '.mf'
                packageDir += '/'

            self.packageFilename = packageDir + self.packageBasename
            self.packageDesc = packageDir + self.packageDesc
            self.packageImportDesc = packageDir + self.packageImportDesc

            self.packageFullpath = Filename(self.packager.installDir, self.packageFilename)
            self.packageFullpath.makeDir()

            if self.p3dApplication:
                self.makeP3dInfo()
            self.multifile.repack()
            self.multifile.close()

            if not self.p3dApplication:
                # The "base" package file is the bottom of the patch chain.
                packageBaseFullpath = Filename(self.packageFullpath + '.base')
                if not packageBaseFullpath.exists() and \
                   self.packageFullpath.exists():
                    # There's a previous version of the package file.
                    # It becomes the "base".
                    self.packageFullpath.renameTo(packageBaseFullpath)

            if not multifileFilename.renameTo(self.packageFullpath):
                self.notify.error("Cannot move %s to %s" % (multifileFilename, self.packageFullpath))

            if self.p3dApplication:
                # No patches for an application; just move it into place.
                # Make the application file executable.
                os.chmod(self.packageFullpath.toOsSpecific(), 0755)
            else:
                self.compressMultifile()
                self.readDescFile()
                self.writeDescFile()
                self.writeImportDescFile()

                # Replace or add the entry in the contents.
                pe = Packager.PackageEntry()
                pe.fromFile(self.packageName, self.platform, self.version,
                            False, self.packager.installDir,
                            self.packageDesc, self.packageImportDesc)
                
                self.packager.contents[pe.getKey()] = pe
                self.packager.contentsChanged = True

            self.cleanup()

        def installSolo(self):
            """ Installs the package as a "solo", which means we
            simply copy the one file into the install directory.  This
            is primarily intended for the "coreapi" plugin, which is
            just a single dll and a jpg file; but it can support other
            kinds of similar "solo" packages as well. """

            self.considerPlatform()

            packageDir = self.packageName
            if self.platform:
                packageDir += '/' + self.platform
            if self.version:
                packageDir += '/' + self.version

            installPath = Filename(self.packager.installDir, packageDir)
            # Remove any files already in the installPath.
            origFiles = vfs.scanDirectory(installPath)
            if origFiles:
                for origFile in origFiles:
                    origFile.getFilename().unlink()

            files = []
            for file in self.files:
                if file.isExcluded(self):
                    # Skip this file.
                    continue
                files.append(file)

            if not files:
                # No files, never mind.
                return

            if len(files) != 1:
                raise PackagerError, 'Multiple files in "solo" package %s' % (self.packageName)
            
            Filename(installPath, '').makeDir()

            file = files[0]
            targetPath = Filename(installPath, file.newName)
            targetPath.setBinary()
            file.filename.setBinary()
            if not file.filename.copyTo(targetPath):
                self.notify.warning("Could not copy %s to %s" % (
                    file.filename, targetPath))

            # Replace or add the entry in the contents.
            pe = Packager.PackageEntry()
            pe.fromFile(self.packageName, self.platform, self.version,
                        True, self.packager.installDir,
                        Filename(packageDir, file.newName), None)
            self.packager.contents[pe.getKey()] = pe
            self.packager.contentsChanged = True

            self.cleanup()
               
        def cleanup(self):
            # Now that all the files have been packed, we can delete
            # the temporary files.
            for file in self.files:
                if file.deleteTemp:
                    file.filename.unlink()

        def addFile(self, *args, **kw):
            """ Adds the named file to the package. """

            file = Packager.PackFile(self, *args, **kw)
            if file.filename in self.sourceFilenames:
                # Don't bother, it's already here.
                return

            if file.newName in self.targetFilenames:
                # Another file is already in the same place.
                file2 = self.targetFilenames[file.newName]
                self.packager.notify.warning(
                    "%s is shadowing %s" % (file2.filename, file.filename))
                return

            self.sourceFilenames[file.filename] = file

            if file.text is None and not file.filename.exists():
                if not file.isExcluded(self):
                    self.packager.notify.warning("No such file: %s" % (file.filename))
                return
            
            self.files.append(file)
            self.targetFilenames[file.newName] = file

        def excludeFile(self, filename):
            """ Excludes the named file (or glob pattern) from the
            package. """
            xfile = Packager.ExcludeFilename(filename, self.packager.caseSensitive)
            self.excludedFilenames.append(xfile)

        def __addImplicitDependenciesWindows(self):
            """ Walks through the list of files, looking for dll's and
            exe's that might include implicit dependencies on other
            dll's.  Tries to determine those dependencies, and adds
            them back into the filelist. """

            # We walk through the list as we modify it.  That's OK,
            # because we want to follow the transitive closure of
            # dependencies anyway.
            for file in self.files:
                if not file.executable:
                    continue
                
                if file.isExcluded(self):
                    # Skip this file.
                    continue

                tempFile = Filename.temporary('', 'p3d_', '.txt')
                command = 'dumpbin /dependents "%s" >"%s"' % (
                    file.filename.toOsSpecific(),
                    tempFile.toOsSpecific())
                try:
                    os.system(command)
                except:
                    pass
                filenames = None

                if tempFile.exists():
                    filenames = self.__parseDependenciesWindows(tempFile)
                    tempFile.unlink()
                if filenames is None:
                    self.notify.warning("Unable to determine dependencies from %s" % (file.filename))
                    continue

                # Attempt to resolve the dependent filename relative
                # to the original filename, before we resolve it along
                # the PATH.
                path = DSearchPath(Filename(file.filename.getDirname()))

                for filename in filenames:
                    filename = Filename.fromOsSpecific(filename)
                    filename.resolveFilename(path)
                    self.addFile(filename, newName = filename.getBasename(),
                                 explicit = False, executable = True)
                    
        def __parseDependenciesWindows(self, tempFile):
            """ Reads the indicated temporary file, the output from
            dumpbin /dependents, to determine the list of dll's this
            executable file depends on. """

            lines = open(tempFile.toOsSpecific(), 'rU').readlines()
            li = 0
            while li < len(lines):
                line = lines[li]
                li += 1
                if line.find(' has the following dependencies') != -1:
                    break

            if li < len(lines):
                line = lines[li]
                if line.strip() == '':
                    # Skip a blank line.
                    li += 1

            # Now we're finding filenames, until the next blank line.
            filenames = []
            while li < len(lines):
                line = lines[li]
                li += 1
                line = line.strip()
                if line == '':
                    # We're done.
                    return filenames
                filenames.append(line)

            # Hmm, we ran out of data.  Oh well.
            if not filenames:
                # Some parse error.
                return None

            # At least we got some data.
            return filenames

        def __addImplicitDependenciesOSX(self):
            """ Walks through the list of files, looking for dylib's
            and executables that might include implicit dependencies
            on other dylib's.  Tries to determine those dependencies,
            and adds them back into the filelist. """

            # We walk through the list as we modify it.  That's OK,
            # because we want to follow the transitive closure of
            # dependencies anyway.
            for file in self.files:
                if not file.executable:
                    continue
                
                if file.isExcluded(self):
                    # Skip this file.
                    continue

                tempFile = Filename.temporary('', 'p3d_', '.txt')
                command = 'otool -L "%s" >"%s"' % (
                    file.filename.toOsSpecific(),
                    tempFile.toOsSpecific())
                try:
                    os.system(command)
                except:
                    pass
                filenames = None

                if tempFile.exists():
                    filenames = self.__parseDependenciesOSX(tempFile)
                    tempFile.unlink()
                if filenames is None:
                    self.notify.warning("Unable to determine dependencies from %s" % (file.filename))
                    continue

                # Attempt to resolve the dependent filename relative
                # to the original filename, before we resolve it along
                # the PATH.
                path = DSearchPath(Filename(file.filename.getDirname()))

                for filename in filenames:
                    filename = Filename.fromOsSpecific(filename)
                    filename.resolveFilename(path)
                    self.addFile(filename, newName = filename.getBasename(),
                                 explicit = False, executable = True)
                    
        def __parseDependenciesOSX(self, tempFile):
            """ Reads the indicated temporary file, the output from
            otool -L, to determine the list of dylib's this
            executable file depends on. """

            lines = open(tempFile.toOsSpecific(), 'rU').readlines()

            filenames = []
            for line in lines:
                if line[0] not in string.whitespace:
                    continue
                line = line.strip()
                if line.startswith('/System/'):
                    continue
                s = line.find(' (compatibility')
                if s != -1:
                    line = line[:s]
                else:
                    s = line.find('.dylib')
                    if s != -1:
                        line = line[:s + 6]
                    else:
                        continue
                filenames.append(line)

            return filenames

        def __addImplicitDependenciesPosix(self):
            """ Walks through the list of files, looking for so's
            and executables that might include implicit dependencies
            on other so's.  Tries to determine those dependencies,
            and adds them back into the filelist. """

            # We walk through the list as we modify it.  That's OK,
            # because we want to follow the transitive closure of
            # dependencies anyway.
            for file in self.files:
                if not file.executable:
                    continue
                
                if file.isExcluded(self):
                    # Skip this file.
                    continue

                tempFile = Filename.temporary('', 'p3d_', '.txt')
                command = 'ldd "%s" >"%s"' % (
                    file.filename.toOsSpecific(),
                    tempFile.toOsSpecific())
                try:
                    os.system(command)
                except:
                    pass
                filenames = None

                if tempFile.exists():
                    filenames = self.__parseDependenciesPosix(tempFile)
                    tempFile.unlink()
                if filenames is None:
                    self.notify.warning("Unable to determine dependencies from %s" % (file.filename))
                    continue

                # Attempt to resolve the dependent filename relative
                # to the original filename, before we resolve it along
                # the PATH.
                path = DSearchPath(Filename(file.filename.getDirname()))

                for filename in filenames:
                    filename = Filename.fromOsSpecific(filename)
                    filename.resolveFilename(path)
                    self.addFile(filename, newName = filename.getBasename(),
                                 explicit = False, executable = True)
                    
        def __parseDependenciesPosix(self, tempFile):
            """ Reads the indicated temporary file, the output from
            ldd, to determine the list of so's this executable file
            depends on. """

            lines = open(tempFile.toOsSpecific(), 'rU').readlines()

            filenames = []
            for line in lines:
                line = line.strip()
                s = line.find(' => ')
                if s == -1:
                    continue

                line = line[:s].strip()
                filenames.append(line)

            return filenames

        def addExtensionModules(self):
            """ Adds the extension modules detected by the freezer to
            the current list of files. """

            freezer = self.freezer
            for moduleName, filename in freezer.extras:
                filename = Filename.fromOsSpecific(filename)
                newName = filename.getBasename()
                if '.' in moduleName:
                    newName = '/'.join(moduleName.split('.')[:-1])
                    newName += '/' + filename.getBasename()
                # Sometimes the PYTHONPATH has the wrong case in it.
                filename.makeTrueCase()
                self.addFile(filename, newName = newName,
                             explicit = False, extract = True,
                             executable = True,
                             platformSpecific = True)
            freezer.extras = []


        def makeP3dInfo(self):
            """ Makes the p3d_info.xml file that defines the
            application startup parameters and such. """

            doc = TiXmlDocument()
            decl = TiXmlDeclaration("1.0", "utf-8", "")
            doc.InsertEndChild(decl)

            xpackage = TiXmlElement('package')
            xpackage.SetAttribute('name', self.packageName)
            if self.platform:
                xpackage.SetAttribute('platform', self.platform)
            if self.version:
                xpackage.SetAttribute('version', self.version)

            xpackage.SetAttribute('main_module', self.mainModule[1])

            self.__addConfigs(xpackage)

            for package in self.requires:
                xrequires = TiXmlElement('requires')
                xrequires.SetAttribute('name', package.packageName)
                if package.version:
                    xrequires.SetAttribute('version', package.version)
                xrequires.SetAttribute('host', package.host)
                xpackage.InsertEndChild(xrequires)

            doc.InsertEndChild(xpackage)

            # Write the xml file to a temporary file on disk, so we
            # can add it to the multifile.
            filename = Filename.temporary('', 'p3d_', '.xml')
            doc.SaveFile(filename.toOsSpecific())

            # It's important not to compress this file: the core API
            # runtime can't decode compressed subfiles.
            self.multifile.addSubfile('p3d_info.xml', filename, 0)
            
            self.multifile.flush()
            filename.unlink()
            

        def compressMultifile(self):
            """ Compresses the .mf file into an .mf.pz file. """

            compressedName = self.packageFilename + '.pz'
            compressedPath = Filename(self.packager.installDir, compressedName)
            if not compressFile(self.packageFullpath, compressedPath, 6):
                message = 'Unable to write %s' % (compressedPath)
                raise PackagerError, message

        def readDescFile(self):
            """ Reads the existing package.xml file before rewriting
            it.  We need this to preserve the list of patchfiles
            between sessions. """

            self.patchVersion = '1'
            self.patches = []
            
            packageDescFullpath = Filename(self.packager.installDir, self.packageDesc)
            doc = TiXmlDocument(packageDescFullpath.toOsSpecific())
            if not doc.LoadFile():
                return
            
            xpackage = doc.FirstChildElement('package')
            if not xpackage:
                return

            patchVersion = xpackage.Attribute('patch_version')
            if not patchVersion:
                patchVersion = xpackage.Attribute('last_patch_version')
            if patchVersion:
                self.patchVersion = patchVersion

            xpatch = xpackage.FirstChildElement('patch')
            while xpatch:
                self.patches.append(xpatch.Clone())
                xpatch = xpatch.NextSiblingElement('patch')

        def writeDescFile(self):
            """ Makes the package.xml file that describes the package
            and its contents, for download. """

            packageDescFullpath = Filename(self.packager.installDir, self.packageDesc)
            doc = TiXmlDocument(packageDescFullpath.toOsSpecific())
            decl = TiXmlDeclaration("1.0", "utf-8", "")
            doc.InsertEndChild(decl)

            xpackage = TiXmlElement('package')
            xpackage.SetAttribute('name', self.packageName)
            if self.platform:
                xpackage.SetAttribute('platform', self.platform)
            if self.version:
                xpackage.SetAttribute('version', self.version)

            xpackage.SetAttribute('last_patch_version', self.patchVersion)

            self.__addConfigs(xpackage)

            for package in self.requires:
                xrequires = TiXmlElement('requires')
                xrequires.SetAttribute('name', package.packageName)
                if self.platform and package.platform:
                    xrequires.SetAttribute('platform', package.platform)
                if package.version:
                    xrequires.SetAttribute('version', package.version)
                xrequires.SetAttribute('host', package.host)
                xpackage.InsertEndChild(xrequires)

            xuncompressedArchive = self.getFileSpec(
                'uncompressed_archive', self.packageFullpath,
                self.packageBasename)
            xpackage.InsertEndChild(xuncompressedArchive)

            xcompressedArchive = self.getFileSpec(
                'compressed_archive', self.packageFullpath + '.pz',
                self.packageBasename + '.pz')
            xpackage.InsertEndChild(xcompressedArchive)

            packageBaseFullpath = Filename(self.packageFullpath + '.base')
            if packageBaseFullpath.exists():
                xbaseVersion = self.getFileSpec(
                    'base_version', packageBaseFullpath,
                    self.packageBasename + '.base')
                xpackage.InsertEndChild(xbaseVersion)

            # Copy in the patch entries read from the previous version
            # of the desc file.
            for xpatch in self.patches:
                xpackage.InsertEndChild(xpatch)

            self.extracts.sort()
            for name, xextract in self.extracts:
                xpackage.InsertEndChild(xextract)

            doc.InsertEndChild(xpackage)
            doc.SaveFile()

        def __addConfigs(self, xpackage):
            """ Adds the XML config values defined in self.configs to
            the indicated XML element. """

            if self.configs:
                xconfig = TiXmlElement('config')

                for variable, value in self.configs.items():
                    if isinstance(value, types.UnicodeType):
                        xconfig.SetAttribute(variable, value.encode('utf-8'))
                    elif isinstance(value, types.BooleanType):
                        # True or False must be encoded as 1 or 0.
                        xconfig.SetAttribute(variable, str(int(value)))
                    else:
                        xconfig.SetAttribute(variable, str(value))
                        
                xpackage.InsertEndChild(xconfig)

        def writeImportDescFile(self):
            """ Makes the package.import.xml file that describes the
            package and its contents, for other packages and
            applications that may wish to "require" this one. """
        
            packageImportDescFullpath = Filename(self.packager.installDir, self.packageImportDesc)
            doc = TiXmlDocument(packageImportDescFullpath.toOsSpecific())
            decl = TiXmlDeclaration("1.0", "utf-8", "")
            doc.InsertEndChild(decl)

            xpackage = TiXmlElement('package')
            xpackage.SetAttribute('name', self.packageName)
            if self.platform:
                xpackage.SetAttribute('platform', self.platform)
            if self.version:
                xpackage.SetAttribute('version', self.version)
            xpackage.SetAttribute('host', self.host)

            for package in self.requires:
                xrequires = TiXmlElement('requires')
                xrequires.SetAttribute('name', package.packageName)
                if self.platform and package.platform:
                    xrequires.SetAttribute('platform', package.platform)
                if package.version:
                    xrequires.SetAttribute('version', package.version)
                xrequires.SetAttribute('host', package.host)
                xpackage.InsertEndChild(xrequires)

            self.components.sort()
            for type, name, xcomponent in self.components:
                xpackage.InsertEndChild(xcomponent)

            doc.InsertEndChild(xpackage)
            doc.SaveFile()

        def readImportDescFile(self, filename):
            """ Reads the import desc file.  Returns True on success,
            False on failure. """

            doc = TiXmlDocument(filename.toOsSpecific())
            if not doc.LoadFile():
                return False
            xpackage = doc.FirstChildElement('package')
            if not xpackage:
                return False

            self.packageName = xpackage.Attribute('name')
            self.platform = xpackage.Attribute('platform')
            self.version = xpackage.Attribute('version')
            self.host = xpackage.Attribute('host')

            self.requires = []
            xrequires = xpackage.FirstChildElement('requires')
            while xrequires:
                packageName = xrequires.Attribute('name')
                platform = xrequires.Attribute('platform')
                version = xrequires.Attribute('version')
                host = xrequires.Attribute('host')
                if packageName:
                    package = self.packager.findPackage(
                        packageName, platform = platform, version = version,
                        host = host, requires = self.requires)
                    if package:
                        self.requires.append(package)
                xrequires = xrequires.NextSiblingElement('requires')

            self.targetFilenames = {}
            xcomponent = xpackage.FirstChildElement('component')
            while xcomponent:
                name = xcomponent.Attribute('filename')
                if name:
                    self.targetFilenames[name] = True
                xcomponent = xcomponent.NextSiblingElement('component')

            self.moduleNames = {}
            xmodule = xpackage.FirstChildElement('module')
            while xmodule:
                moduleName = xmodule.Attribute('name')
                exclude = int(xmodule.Attribute('exclude') or 0)
                forbid = int(xmodule.Attribute('forbid') or 0)
                allowChildren = int(xmodule.Attribute('allowChildren') or 0)
                
                if moduleName:
                    mdef = FreezeTool.Freezer.ModuleDef(
                        moduleName, exclude = exclude, forbid = forbid,
                        allowChildren = allowChildren)
                    self.moduleNames[moduleName] = mdef
                xmodule = xmodule.NextSiblingElement('module')

            return True

        def getFileSpec(self, element, pathname, newName):
            """ Returns an xcomponent or similar element with the file
            information for the indicated file. """
            
            xspec = TiXmlElement(element)

            size = pathname.getFileSize()
            timestamp = pathname.getTimestamp()

            hv = HashVal()
            hv.hashFile(pathname)
            hash = hv.asHex()

            xspec.SetAttribute('filename', newName)
            xspec.SetAttribute('size', str(size))
            xspec.SetAttribute('timestamp', str(timestamp))
            xspec.SetAttribute('hash', hash)

            return xspec

            

        def addPyFile(self, file):
            """ Adds the indicated python file, identified by filename
            instead of by module name, to the package. """

            # Convert the raw filename back to a module name, so we
            # can see if we've already loaded this file.  We assume
            # that all Python files within the package will be rooted
            # at the top of the package.

            filename = file.newName.rsplit('.', 1)[0]
            moduleName = filename.replace("/", ".")
            if moduleName.endswith('.__init__'):
                moduleName = moduleName.rsplit('.', 1)[0]

            if moduleName in self.freezer.modules:
                # This Python file is already known.  We don't have to
                # deal with it again.
                return

            self.freezer.addModule(moduleName, newName = moduleName,
                                   filename = file.filename)

        def addEggFile(self, file):
            # Precompile egg files to bam's.
            np = self.packager.loader.loadModel(file.filename)
            if not np:
                raise StandardError, 'Could not read egg file %s' % (file.filename)

            bamName = Filename(file.newName)
            bamName.setExtension('bam')
            self.addNode(np.node(), file.filename, bamName.cStr())

        def addBamFile(self, file):
            # Load the bam file so we can massage its textures.
            bamFile = BamFile()
            if not bamFile.openRead(file.filename):
                raise StandardError, 'Could not read bam file %s' % (file.filename)

            if not bamFile.resolve():
                raise StandardError, 'Could not resolve bam file %s' % (file.filename)

            node = bamFile.readNode()
            if not node:
                raise StandardError, 'Not a model file: %s' % (file.filename)

            self.addNode(node, file.filename, file.newName)

        def addNode(self, node, filename, newName):
            """ Converts the indicated node to a bam stream, and adds the
            bam file to the multifile under the indicated newName. """

            # If the Multifile already has a file by this name, don't
            # bother adding it again.
            if self.multifile.findSubfile(newName) >= 0:
                return

            # Be sure to import all of the referenced textures, and tell
            # them their new location within the multifile.

            for tex in NodePath(node).findAllTextures():
                if not tex.hasFullpath() and tex.hasRamImage():
                    # We need to store this texture as a raw-data image.
                    # Clear the newName so this will happen
                    # automatically.
                    tex.clearFilename()
                    tex.clearAlphaFilename()

                else:
                    # We can store this texture as a file reference to its
                    # image.  Copy the file into our multifile, and rename
                    # its reference in the texture.
                    if tex.hasFilename():
                        tex.setFilename(self.addFoundTexture(tex.getFullpath()))
                    if tex.hasAlphaFilename():
                        tex.setAlphaFilename(self.addFoundTexture(tex.getAlphaFullpath()))

            # Now generate an in-memory bam file.  Tell the bam writer to
            # keep the textures referenced by their in-multifile path.
            bamFile = BamFile()
            stream = StringStream()
            bamFile.openWrite(stream)
            bamFile.getWriter().setFileTextureMode(bamFile.BTMUnchanged)
            bamFile.writeObject(node)
            bamFile.close()

            # Clean the node out of memory.
            node.removeAllChildren()

            # Now we have an in-memory bam file.
            stream.seekg(0)
            self.multifile.addSubfile(newName, stream, self.compressionLevel)

            # Flush it so the data gets written to disk immediately, so we
            # don't have to keep it around in ram.
            self.multifile.flush()
            
            xcomponent = TiXmlElement('component')
            xcomponent.SetAttribute('filename', newName)
            self.components.append(('c', newName.lower(), xcomponent))

        def addFoundTexture(self, filename):
            """ Adds the newly-discovered texture to the output, if it has
            not already been included.  Returns the new name within the
            package tree. """

            filename = Filename(filename)
            filename.makeCanonical()

            file = self.sourceFilenames.get(filename, None)
            if file:
                # Never mind, it's already on the list.
                return file.newName

            # We have to copy the image into the plugin tree somewhere.
            newName = self.importedMapsDir + '/' + filename.getBasename()
            uniqueId = 0
            while newName in self.targetFilenames:
                uniqueId += 1
                newName = '%s/%s_%s.%s' % (
                    self.importedMapsDir, filename.getBasenameWoExtension(),
                    uniqueId, filename.getExtension())

            self.addFile(filename, newName = newName, explicit = False,
                         compress = False)
            return newName

        def addDcFile(self, file):
            """ Adds a dc file to the archive.  A dc file gets its
            internal comments and parameter names stripped out of the
            final result automatically.  This is as close as we can
            come to "compiling" a dc file, since all of the remaining
            symbols are meaningful at runtime. """
            
            # First, read in the dc file
            dcFile = DCFile()
            if not dcFile.read(file.filename):
                self.notify.error("Unable to parse %s." % (file.filename))

            # And then write it out without the comments and such.
            stream = StringStream()
            if not dcFile.write(stream, True):
                self.notify.error("Unable to write %s." % (file.filename))

            file.text = stream.getData()
            self.addComponent(file)

        def addDcImports(self, file):
            """ Adds the Python modules named by the indicated dc
            file. """

            dcFile = DCFile()
            if not dcFile.read(file.filename):
                self.notify.error("Unable to parse %s." % (file.filename))

            for n in range(dcFile.getNumImportModules()):
                moduleName = dcFile.getImportModule(n)
                moduleSuffixes = []
                if '/' in moduleName:
                    moduleName, suffixes = moduleName.split('/', 1)
                    moduleSuffixes = suffixes.split('/')
                self.freezer.addModule(moduleName)

                for suffix in self.packager.dcClientSuffixes:
                    if suffix in moduleSuffixes:
                        self.freezer.addModule(moduleName + suffix)

                for i in range(dcFile.getNumImportSymbols(n)):
                    symbolName = dcFile.getImportSymbol(n, i)
                    symbolSuffixes = []
                    if '/' in symbolName:
                        symbolName, suffixes = symbolName.split('/', 1)
                        symbolSuffixes = suffixes.split('/')

                    # "from moduleName import symbolName".

                    # Maybe this symbol is itself a module; if that's
                    # the case, we need to add it to the list also.
                    self.freezer.addModule('%s.%s' % (moduleName, symbolName),
                                           implicit = True)
                    for suffix in self.packager.dcClientSuffixes:
                        if suffix in symbolSuffixes:
                            self.freezer.addModule('%s.%s%s' % (moduleName, symbolName, suffix),
                                                   implicit = True)

            
        def addPrcFile(self, file):
            """ Adds a prc file to the archive.  Like the dc file,
            this strips comments and such before adding.  It's also
            possible to set prcEncryptionKey and/or prcSignCommand to
            further manipulate prc files during processing. """

            # First, read it in.
            if file.text:
                textLines = file.text.split('\n')
            else:
                textLines = open(file.filename.toOsSpecific(), 'rU').readlines()

            # Then write it out again, without the comments.
            tempFilename = Filename.temporary('', 'p3d_', '.prc')
            temp = open(tempFilename.toOsSpecific(), 'w')
            for line in textLines:
                line = line.strip()
                if line and line[0] != '#':
                    # Write the line out only if it's not a comment.
                    temp.write(line + '\n')
            temp.close()

            if self.packager.prcSignCommand:
                # Now sign the file.
                command = '%s -n "%s"' % (
                    self.packager.prcSignCommand, tempFilename.toOsSpecific())
                self.notify.info(command)
                exitStatus = os.system(command)
                if exitStatus != 0:
                    self.notify.error('Command failed: %s' % (command))

            if self.packager.prcEncryptionKey:
                # And now encrypt it.
                if file.newName.endswith('.prc'):
                    # Change .prc -> .pre
                    file.newName = file.newName[:-1] + 'e'
                
                preFilename = Filename.temporary('', 'p3d_', '.pre')
                encryptFile(tempFilename, preFilename, self.packager.prcEncryptionKey)
                tempFilename.unlink()
                tempFilename = preFilename

            if file.deleteTemp:
                file.filename.unlink()

            file.filename = tempFilename
            file.text = None
            file.deleteTemp = True

            self.addComponent(file)

        def addComponent(self, file):
            compressionLevel = 0
            if file.compress:
                compressionLevel = self.compressionLevel

            if file.text:
                stream = StringStream(file.text)
                self.multifile.addSubfile(file.newName, stream, compressionLevel)
                self.multifile.flush()
            else:
                self.multifile.addSubfile(file.newName, file.filename, compressionLevel)
            if file.extract:
                xextract = self.getFileSpec('extract', file.filename, file.newName)
                self.extracts.append((file.newName.lower(), xextract))

            xcomponent = TiXmlElement('component')
            xcomponent.SetAttribute('filename', file.newName)
            self.components.append(('c', file.newName.lower(), xcomponent))

        def requirePackage(self, package):
            """ Indicates a dependency on the given package.  This
            also implicitly requires all of the package's requirements
            as well. """

            for p2 in package.requires + [package]:
                if p2 not in self.requires:
                    self.requires.append(p2)
                    for filename in p2.targetFilenames.keys():
                        self.skipFilenames[filename] = True
                    for moduleName, mdef in p2.moduleNames.items():
                        self.skipModules[moduleName] = mdef

    # Packager constructor
    def __init__(self):

        # The following are config settings that the caller may adjust
        # before calling any of the command methods.

        # These should each be a Filename, or None if they are not
        # filled in.
        self.installDir = None
        self.persistDir = None

        # The download URL at which these packages will eventually be
        # hosted.
        self.host = PandaSystem.getPackageHostUrl()
        self.hostDescriptiveName = None

        # A search list for previously-built local packages.
        self.installSearch = ConfigVariableSearchPath('pdef-path')

        # The system PATH, for searching dll's and exe's.
        self.executablePath = DSearchPath()
        if PandaSystem.getPlatform().startswith('win'):
            self.addWindowsSearchPath(self.executablePath, "PATH")
        elif PandaSystem.getPlatform().startswith('osx'):
            self.addPosixSearchPath(self.executablePath, "DYLD_LIBRARY_PATH")
            self.addPosixSearchPath(self.executablePath, "LD_LIBRARY_PATH")
            self.addPosixSearchPath(self.executablePath, "PATH")
            self.executablePath.appendDirectory('/lib')
            self.executablePath.appendDirectory('/usr/lib')
        else:
            self.addPosixSearchPath(self.executablePath, "LD_LIBRARY_PATH")
            self.addPosixSearchPath(self.executablePath, "PATH")
            self.executablePath.appendDirectory('/lib')
            self.executablePath.appendDirectory('/usr/lib')

        # The platform string.
        self.platform = PandaSystem.getPlatform()

        # Optional signing and encrypting features.
        self.encryptionKey = None
        self.prcEncryptionKey = None
        self.prcSignCommand = None

        # This is a list of filename extensions and/or basenames that
        # indicate files that should be encrypted within the
        # multifile.  This provides obfuscation only, not real
        # security, since the decryption key must be part of the
        # client and is therefore readily available to any hacker.
        # Not only is this feature useless, but using it also
        # increases the size of your patchfiles, since encrypted files
        # don't patch as tightly as unencrypted files.  But it's here
        # if you really want it.
        self.encryptExtensions = ['ptf', 'dna', 'txt', 'dc']
        self.encryptFiles = []

        # This is the list of DC import suffixes that should be
        # available to the client.  Other suffixes, like AI and UD,
        # are server-side only and should be ignored by the Scrubber.
        self.dcClientSuffixes = ['OV']

        # Is this file system case-sensitive?
        self.caseSensitive = True
        if PandaSystem.getPlatform().startswith('win'):
            self.caseSensitive = False
        elif PandaSystem.getPlatform().startswith('osx'):
            self.caseSensitive = False

        # Get the list of filename extensions that are recognized as
        # image files.
        self.imageExtensions = []
        for type in PNMFileTypeRegistry.getGlobalPtr().getTypes():
            self.imageExtensions += type.getExtensions()

        # Other useful extensions.  The .pz extension is implicitly
        # stripped.

        # Model files.
        self.modelExtensions = [ 'egg', 'bam' ]

        # Text files that are copied (and compressed) to the package
        # without processing.
        self.textExtensions = [ 'prc', 'ptf', 'txt' ]

        # Binary files that are copied (and compressed) without
        # processing.
        self.binaryExtensions = [ 'ttf', 'wav', 'mid' ]

        # Files that represent an executable or shared library.
        if self.platform.startswith('win'):
            self.executableExtensions = [ 'dll', 'pyd', 'exe' ]
        elif self.platform.startswith('osx'):
            self.executableExtensions = [ 'so', 'dylib' ]
        else:
            self.executableExtensions = [ 'so' ]

        # Extensions that are automatically remapped by convention.
        self.remapExtensions = {}
        if self.platform.startswith('win'):
            pass
        elif self.platform.startswith('osx'):
            self.remapExtensions = {
                'dll' : 'dylib',
                'pyd' : 'dylib',
                'exe' : ''
                }
        else:
            self.remapExtensions = {
                'dll' : 'so',
                'pyd' : 'so',
                'exe' : ''
                }

        # Files that should be extracted to disk.
        self.extractExtensions = self.executableExtensions[:]

        # Files that indicate a platform dependency.
        self.platformSpecificExtensions = self.executableExtensions[:]

        # Binary files that are considered uncompressible, and are
        # copied without compression.
        self.uncompressibleExtensions = [ 'mp3', 'ogg' ]

        # Files which are not to be processed further, but which
        # should be added exactly byte-for-byte as they are.
        self.unprocessedExtensions = []

        # System files that should never be packaged.  For
        # case-insensitive filesystems (like Windows), put the
        # lowercase filename here.  Case-sensitive filesystems should
        # use the correct case.
        self.excludeSystemFiles = [
            'kernel32.dll', 'user32.dll', 'wsock32.dll', 'ws2_32.dll',
            'advapi32.dll', 'opengl32.dll', 'glu32.dll', 'gdi32.dll',
            'shell32.dll', 'ntdll.dll', 'ws2help.dll', 'rpcrt4.dll',
            'imm32.dll', 'ddraw.dll', 'shlwapi.dll', 'secur32.dll',
            'dciman32.dll', 'comdlg32.dll', 'comctl32.dll', 'ole32.dll',
            'oleaut32.dll', 'gdiplus.dll', 'winmm.dll',

            'libsystem.b.dylib', 'libmathcommon.a.dylib', 'libmx.a.dylib',
            'libstdc++.6.dylib',
            ]

        # As above, but with filename globbing to catch a range of
        # filenames.
        self.excludeSystemGlobs = [
            GlobPattern('d3dx9_*.dll'),

            GlobPattern('linux-gate.so*'),
            GlobPattern('libdl.so*'),
            GlobPattern('libm.so*'),
            GlobPattern('libc.so*'),
            GlobPattern('libGL.so*'),
            GlobPattern('libGLU.so*'),
            GlobPattern('libX*.so*'),
            ]

        # A Loader for loading models.
        self.loader = Loader.Loader(self)
        self.sfxManagerList = None
        self.musicManager = None

        # This is filled in during readPackageDef().
        self.packageList = []

        # A table of all known packages by name.
        self.packages = {}

        # A list of PackageEntry objects read from the contents.xml
        # file.
        self.contents = {}

    def addWindowsSearchPath(self, searchPath, varname):
        """ Expands $varname, interpreting as a Windows-style search
        path, and adds its contents to the indicated DSearchPath. """

        path = ExecutionEnvironment.getEnvironmentVariable(varname)
        for dirname in path.split(';'):
            dirname = Filename.fromOsSpecific(dirname)
            if dirname.makeTrueCase():
                searchPath.appendDirectory(dirname)

    def addPosixSearchPath(self, searchPath, varname):
        """ Expands $varname, interpreting as a Posix-style search
        path, and adds its contents to the indicated DSearchPath. """

        path = ExecutionEnvironment.getEnvironmentVariable(varname)
        for dirname in path.split(':'):
            dirname = Filename.fromOsSpecific(dirname)
            if dirname.makeTrueCase():
                searchPath.appendDirectory(dirname)


    def setup(self):
        """ Call this method to initialize the class after filling in
        some of the values in the constructor. """

        self.knownExtensions = self.imageExtensions + self.modelExtensions + self.textExtensions + self.binaryExtensions + self.uncompressibleExtensions + self.unprocessedExtensions

        self.currentPackage = None

        # We must have an actual install directory.
        assert(self.installDir)

        if not PandaSystem.getPackageVersionString() or not PandaSystem.getPackageHostUrl():
            raise PackagerError, 'This script must be run using a version of Panda3D that has been built\nfor distribution.  Try using ppackage.p3d or packp3d.p3d instead.'

        self.readContentsFile()

    def close(self):
        """ Called after reading all of the package def files, this
        performs any final cleanup appropriate. """

        self.writeContentsFile()

    def readPackageDef(self, packageDef):
        """ Reads the named .pdef file and constructs the packages
        indicated within it.  Raises an exception if the pdef file is
        invalid.  Returns the list of packages constructed. """

        self.notify.info('Reading %s' % (packageDef))

        # We use exec to "read" the .pdef file.  This has the nice
        # side-effect that the user can put arbitrary Python code in
        # there to control conditional execution, and such.

        # Set up the namespace dictionary for exec.
        globals = {}
        globals['__name__'] = packageDef.getBasenameWoExtension()

        globals['platform'] = PandaSystem.getPlatform()
        globals['packager'] = self

        # We'll stuff all of the predefined functions, and the
        # predefined classes, in the global dictionary, so the pdef
        # file can reference them.

        # By convention, the existence of a method of this class named
        # do_foo(self) is sufficient to define a pdef method call
        # foo().
        for methodName in self.__class__.__dict__.keys():
            if methodName.startswith('do_'):
                name = methodName[3:]
                c = func_closure(name)
                globals[name] = c.generic_func

        globals['p3d'] = class_p3d
        globals['package'] = class_package
        globals['solo'] = class_solo

        # Now exec the pdef file.  Assuming there are no syntax
        # errors, and that the pdef file doesn't contain any really
        # crazy Python code, all this will do is fill in the
        # '__statements' list in the module scope.

        # It appears that having a separate globals and locals
        # dictionary causes problems with resolving symbols within a
        # class scope.  So, we just use one dictionary, the globals.
        execfile(packageDef.toOsSpecific(), globals)

        packages = []

        # Now iterate through the statements and operate on them.
        statements = globals.get('__statements', [])
        if not statements:
            self.notify.info("No packages defined.")
        
        try:
            for (lineno, stype, name, args, kw) in statements:
                if stype == 'class':
                    classDef = globals[name]
                    p3dApplication = (class_p3d in classDef.__bases__)
                    solo = (class_solo in classDef.__bases__)
                    self.beginPackage(name, p3dApplication = p3dApplication,
                                      solo = solo)
                    statements = classDef.__dict__.get('__statements', [])
                    if not statements:
                        self.notify.info("No files added to %s" % (name))
                    for (lineno, stype, name, args, kw) in statements:
                        if stype == 'class':
                            raise PackagerError, 'Nested classes not allowed'
                        self.__evalFunc(name, args, kw)
                    package = self.endPackage()
                    packages.append(package)
                else:
                    self.__evalFunc(name, args, kw)
        except PackagerError:
            # Append the line number and file name to the exception
            # error message.
            inst = sys.exc_info()[1]
            if not inst.args:
                inst.args = ('Error',)
                
            inst.args = (inst.args[0] + ' on line %s of %s' % (lineno, packageDef),)
            raise
                    
        return packages

    def __evalFunc(self, name, args, kw):
        """ This is called from readPackageDef(), above, to call the
        function do_name(*args, **kw), as extracted from the pdef
        file. """
        
        funcname = 'do_%s' % (name)
        func = getattr(self, funcname)
        try:
            func(*args, **kw)
        except OutsideOfPackageError:
            message = '%s encountered outside of package definition' % (name)
            raise OutsideOfPackageError, message

    def __expandTabs(self, line, tabWidth = 8):
        """ Expands tab characters in the line to 8 spaces. """
        p = 0
        while p < len(line):
            if line[p] == '\t':
                # Expand a tab.
                nextStop = ((p + tabWidth) / tabWidth) * tabWidth
                numSpaces = nextStop - p
                line = line[:p] + ' ' * numSpaces + line[p + 1:]
                p = nextStop
            else:
                p += 1

        return line

    def __countLeadingWhitespace(self, line):
        """ Returns the number of leading whitespace characters in the
        line. """

        line = self.__expandTabs(line)
        return len(line) - len(line.lstrip())

    def __stripLeadingWhitespace(self, line, whitespaceCount):
        """ Removes the indicated number of whitespace characters, but
        no more. """

        line = self.__expandTabs(line)
        line = line[:whitespaceCount].lstrip() + line[whitespaceCount:]
        return line

    def __parseArgs(self, words, argList):
        args = {}
        
        while len(words) > 1:
            arg = words[-1]
            if '=' not in arg:
                return args

            parameter, value = arg.split('=', 1)
            parameter = parameter.strip()
            value = value.strip()
            if parameter not in argList:
                message = 'Unknown parameter %s' % (parameter)
                raise PackagerError, message
            if parameter in args:
                message = 'Duplicate parameter %s' % (parameter)
                raise PackagerError, message

            args[parameter] = value

            del words[-1]
                
    
    def beginPackage(self, packageName, p3dApplication = False,
                     solo = False):
        """ Begins a new package specification.  packageName is the
        basename of the package.  Follow this with a number of calls
        to file() etc., and close the package with endPackage(). """

        if self.currentPackage:
            raise PackagerError, 'unclosed endPackage %s' % (self.currentPackage.packageName)

        package = self.Package(packageName, self)
        self.currentPackage = package

        package.p3dApplication = p3dApplication
        package.solo = solo
        
    def endPackage(self):
        """ Closes the current package specification.  This actually
        generates the package file.  Returns the finished package."""
        
        if not self.currentPackage:
            raise PackagerError, 'unmatched endPackage'

        package = self.currentPackage
        package.close()

        self.packageList.append(package)
        self.packages[(package.packageName, package.platform, package.version)] = package
        self.currentPackage = None

        return package

    def findPackage(self, packageName, platform = None, version = None,
                    host = None, requires = None):
        """ Searches for the named package from a previous publish
        operation along the install search path.

        If requires is not None, it is a list of Package objects that
        are already required.  The new Package object must be
        compatible with the existing Packages, or an error is
        returned.  This is also useful for determining the appropriate
        package version to choose when a version is not specified.

        Returns the Package object, or None if the package cannot be
        located. """

        if not platform:
            platform = self.platform

        # Is it a package we already have resident?
        package = self.packages.get((packageName, platform, version, host), None)
        if package:
            return package

        # Look on the searchlist.
        for dirname in self.installSearch.getDirectories():
            package = self.__scanPackageDir(dirname, packageName, platform, version, host, requires = requires)
            if not package:
                package = self.__scanPackageDir(dirname, packageName, None, version, host, requires = requires)

            if package:
                break

        if not package:
            # Query the indicated host.
            package = self.__findPackageOnHost(packageName, platform, version, host, requires = requires)

        if package:
            package = self.packages.setdefault((package.packageName, package.platform, package.version, package.host), package)
            self.packages[(packageName, platform, version, host)] = package
            return package
                
        return None

    def __scanPackageDir(self, rootDir, packageName, platform, version,
                         host, requires = None):
        """ Scans a directory on disk, looking for *.import.xml files
        that match the indicated packageName and optional version.  If a
        suitable xml file is found, reads it and returns the assocated
        Package definition.

        If a version is not specified, and multiple versions are
        available, the highest-numbered version that matches will be
        selected.
        """

        packageDir = Filename(rootDir, packageName)
        basename = packageName

        if version:
            # A specific version package.
            packageDir = Filename(packageDir, version)
            basename += '.%s' % (version)
        else:
            # Scan all versions.
            packageDir = Filename(packageDir, '*')
            basename += '.%s' % ('*')

        if platform:
            packageDir = Filename(packageDir, platform)
            basename += '.%s' % (platform)

        # Actually, the host means little for this search, since we're
        # only looking in a local directory at this point.

        basename += '.import.xml'
        filename = Filename(packageDir, basename)
        filelist = glob.glob(filename.toOsSpecific())
        if not filelist:
            # It doesn't exist in the nested directory; try the root
            # directory.
            filename = Filename(rootDir, basename)
            filelist = glob.glob(filename.toOsSpecific())

        packages = []
        for file in filelist:
            package = self.__readPackageImportDescFile(Filename.fromOsSpecific(file))
            packages.append(package)

        self.__sortImportPackages(packages)
        for package in packages:
            if package and self.__packageIsValid(package, requires):
                return package

        return None

    def __findPackageOnHost(self, packageName, platform, version, hostUrl, requires = None):
        appRunner = AppRunnerGlobal.appRunner
        if not appRunner:
            # We don't download import files from a host unless we're
            # running in a packaged environment ourselves.  It would
            # be possible to do this, but a fair bit of work for not
            # much gain--this is meant to be run in a packaged
            # environment.
            return None

        host = appRunner.getHost(hostUrl)
        package = host.getPackage(packageName, version, platform = platform)
        if not package or not package.importDescFile:
            return None

        # Now we've retrieved a PackageInfo.  Get the import desc file
        # from it.
        filename = Filename(host.importsDir, package.importDescFile.basename)
        if not appRunner.freshenFile(host, package.importDescFile, filename):
            self.notify.error("Couldn't download import file.")
            return None

        # Now that we have the import desc file, use it to load one of
        # our Package objects.
        package = self.Package('', self)
        if package.readImportDescFile(filename):
            return package

    def __sortImportPackages(self, packages):
        """ Given a list of Packages read from *.import.xml filenames,
        sorts them in reverse order by version, so that the
        highest-numbered versions appear first in the list. """

        tuples = []
        for package in packages:
            version = self.__makeVersionTuple(package.version)
            tuples.append((version, file))
        tuples.sort(reverse = True)

        return map(lambda t: t[1], tuples)

    def __makeVersionTuple(self, version):
        """ Converts a version string into a tuple for sorting, by
        separating out numbers into separate numeric fields, so that
        version numbers sort numerically where appropriate. """

        words = []
        p = 0
        while p < len(version):
            # Scan to the first digit.
            w = ''
            while p < len(version) and version[p] not in string.digits:
                w += version[p]
                p += 1
            words.append(w)

            # Scan to the end of the string of digits.
            w = ''
            while p < len(version) and version[p] in string.digits:
                w += version[p]
                p += 1
            if w:
                words.append(int(w))

        return tuple(words)

    def __packageIsValid(self, package, requires):
        """ Returns true if the package is valid, meaning it can be
        imported without conflicts with existing packages already
        required (such as different versions of panda3d). """

        if not requires:
            return True

        # Really, we only check the panda3d package.  The other
        # packages will list this as a dependency, and this is all
        # that matters.

        panda1 = self.__findPackageInList('panda3d', [package] + package.requires)
        panda2 = self.__findPackageInList('panda3d', requires)

        if not panda1 or not panda2:
            return True

        if panda1.version == panda2.version:
            return True

        return False

    def __findPackageInList(self, packageName, list):
        """ Returns the first package with the indicated name in the list. """
        for package in list:
            if package.packageName == packageName:
                return package

        return None

    def __readPackageImportDescFile(self, filename):
        """ Reads the named xml file as a Package, and returns it if
        valid, or None otherwise. """

        package = self.Package('', self)
        if package.readImportDescFile(filename):
            return package

        return None

    def do_config(self, **kw):
        """ Called with any number of keyword parameters.  For each
        keyword parameter, sets the corresponding p3d config variable
        to the given value.  This will be written into the
        p3d_info.xml file at the top of the application, or to the
        package desc file for a package file. """

        if not self.currentPackage:
            raise OutsideOfPackageError

        for keyword, value in kw.items():
            self.currentPackage.configs[keyword] = value

    def do_require(self, *args, **kw):
        """ Indicates a dependency on the named package(s), supplied
        as a name.

        Attempts to install this package will implicitly install the
        named package also.  Files already included in the named
        package will be omitted from this one when building it. """

        if not self.currentPackage:
            raise OutsideOfPackageError

        version = kw.get('version', None)
        host = kw.get('host', None)

        for key in ['version', 'host']:
            if key in kw:
                del kw['version']
        if kw:
            message = "do_require() got an unexpected keyword argument '%s'" % (kw.keys()[0])
            raise TypeError, message

        for packageName in args:
            # A special case when requiring the "panda3d" package.  We
            # supply the version number what we've been compiled with as a
            # default.
            pversion = version
            phost = host
            if packageName == 'panda3d':
                if pversion is None:
                    pversion = PandaSystem.getPackageVersionString()
                if phost is None:
                    phost = PandaSystem.getPackageHostUrl()

            package = self.findPackage(packageName, version = pversion, host = phost,
                                       requires = self.currentPackage.requires)
            if not package:
                message = 'Unknown package %s, version "%s"' % (packageName, version)
                raise PackagerError, message

            self.requirePackage(package)

    def requirePackage(self, package):
        """ Indicates a dependency on the indicated package, supplied
        as a Package object.

        Attempts to install this package will implicitly install the
        named package also.  Files already included in the named
        package will be omitted from this one. """

        if not self.currentPackage:
            raise OutsideOfPackageError

        # A special case when requiring the "panda3d" package.  We
        # complain if the version number doesn't match what we've been
        # compiled with.
        if package.packageName == 'panda3d':
            if package.version != PandaSystem.getPackageVersionString():
                self.notify.warning("Requiring panda3d version %s, which does not match the current build of Panda, which is version %s." % (package, PandaSystem.getPackageVersionString()))
            elif package.host != PandaSystem.getPackageHostUrl():
                self.notify.warning("Requiring panda3d host %s, which does not match the current build of Panda, which is host %s." % (package, PandaSystem.getPackageHostUrl()))

        self.currentPackage.requirePackage(package)

    def do_module(self, *args):
        """ Adds the indicated Python module(s) to the current package. """

        if not self.currentPackage:
            raise OutsideOfPackageError

        for moduleName in args:
            self.currentPackage.freezer.addModule(moduleName)

    def do_renameModule(self, moduleName, newName):
        """ Adds the indicated Python module to the current package,
        renaming to a new name. """

        if not self.currentPackage:
            raise OutsideOfPackageError

        self.currentPackage.freezer.addModule(moduleName, newName = newName)

    def do_excludeModule(self, *args):
        """ Marks the indicated Python module as not to be included. """

        if not self.currentPackage:
            raise OutsideOfPackageError

        for moduleName in args:
            self.currentPackage.freezer.excludeModule(moduleName)

    def do_mainModule(self, moduleName, newName = None, filename = None):
        """ Names the indicated module as the "main" module of the
        application or exe. """

        if not self.currentPackage:
            raise OutsideOfPackageError

        if self.currentPackage.mainModule and self.currentPackage.mainModule[0] != moduleName:
            self.notify.warning("Replacing mainModule %s with %s" % (
                self.currentPackage.mainModule[0], moduleName))

        if not newName:
            newName = moduleName

        if filename:
            newFilename = Filename('/'.join(moduleName.split('.')))
            newFilename.setExtension(filename.getExtension())
            self.currentPackage.addFile(
                filename, newName = newFilename.cStr(),
                deleteTemp = True, explicit = True, extract = True)

        self.currentPackage.mainModule = (moduleName, newName)

    def do_freeze(self, filename, compileToExe = False):
        """ Freezes all of the current Python code into either an
        executable (if compileToExe is true) or a dynamic library (if
        it is false).  The resulting compiled binary is added to the
        current package under the indicated filename.  The filename
        should not include an extension; that will be added. """

        if not self.currentPackage:
            raise OutsideOfPackageError

        package = self.currentPackage
        freezer = package.freezer

        if package.mainModule and not compileToExe:
            self.notify.warning("Ignoring main_module for dll %s" % (filename))
            package.mainModule = None
        if not package.mainModule and compileToExe:
            message = "No main_module specified for exe %s" % (filename)
            raise PackagerError, message

        if package.mainModule:
            moduleName, newName = package.mainModule
            if compileToExe:
                # If we're producing an exe, the main module must
                # be called "__main__".
                newName = '__main__'
                package.mainModule = (moduleName, newName)

            if newName not in freezer.modules:
                freezer.addModule(moduleName, newName = newName)
            else:
                freezer.modules[newName] = freezer.modules[moduleName]
        freezer.done(compileToExe = compileToExe)

        dirname = ''
        basename = filename
        if '/' in basename:
            dirname, basename = filename.rsplit('/', 1)
            dirname += '/'

        basename = freezer.generateCode(basename, compileToExe = compileToExe)

        package.addFile(Filename(basename), newName = dirname + basename,
                        deleteTemp = True, explicit = True, extract = True)
        package.addExtensionModules()
        if not package.platform:
            package.platform = PandaSystem.getPlatform()

        # Reset the freezer for more Python files.
        freezer.reset()
        package.mainModule = None

    def do_file(self, *args, **kw):
        """ Adds the indicated file or files to the current package.
        See addFiles(). """

        self.addFiles(args, **kw)

    def addFiles(self, filenames, text = None, newName = None,
                 newDir = None, extract = None, executable = None,
                 deleteTemp = False, literal = False):

        """ Adds the indicated arbitrary files to the current package.

        filenames is a list of Filename or string objects, and each
        may include shell globbing characters.

        Each file is placed in the named directory, or the toplevel
        directory if no directory is specified.

        Certain special behavior is invoked based on the filename
        extension.  For instance, .py files may be automatically
        compiled and stored as Python modules.

        If newDir is not None, it specifies the directory in which the
        file should be placed.  In this case, all files matched by the
        filename expression are placed in the named directory.

        If newName is not None, it specifies a new filename.  In this
        case, newDir is ignored, and the filename expression must
        match only one file.

        If newName and newDir are both None, the file is placed in the
        toplevel directory, regardless of its source directory.

        If text is nonempty, it contains the text of the file.  In
        this case, the filename is not read, but the supplied text is
        used instead.

        If extract is true, the file is explicitly extracted at
        runtime.

        If executable is true, the file is marked as an executable
        filename, for special treatment.

        If deleteTemp is true, the file is a temporary file and will
        be deleted after its contents are copied to the package.

        If literal is true, then the file extension will be respected
        exactly as it appears, and glob characters will not be
        expanded.  If this is false, then .dll or .exe files will be
        renamed to .dylib and no extension on OSX (or .so on Linux);
        and glob characters will be expanded.
        
        """

        if not self.currentPackage:
            raise OutsideOfPackageError

        files = []
        explicit = True
        
        for filename in filenames:
            filename = Filename(filename)

            if literal:
                thisFiles = [filename.toOsSpecific()]

            else:
                ext = filename.getExtension()

                # A special case, since OSX and Linux don't have a
                # standard extension for program files.
                if executable is None and ext == 'exe':
                    executable = True

                newExt = self.remapExtensions.get(ext, None)
                if newExt is not None:
                    filename.setExtension(newExt)

                thisFiles = glob.glob(filename.toOsSpecific())
                if not thisFiles:
                    thisFiles = [filename.toOsSpecific()]

            if len(thisFiles) > 1:
                explicit = False
            files += thisFiles

        prefix = ''
        if newDir is not None:
            prefix = Filename(newDir).cStr()
            if prefix and prefix[-1] != '/':
                prefix += '/'
        
        if newName:
            if len(files) != 1:
                message = 'Cannot install multiple files on target filename %s' % (newName)
                raise PackagerError, message

        if text:
            if len(files) != 1:
                message = 'Cannot install text to multiple files'
                raise PackagerError, message
            if not newName:
                newName = str(filenames[0])

        for filename in files:
            filename = Filename.fromOsSpecific(filename)
            basename = filename.getBasename()
            name = newName
            if not name:
                name = prefix + basename
                
            self.currentPackage.addFile(
                filename, newName = name, extract = extract,
                explicit = explicit, executable = executable,
                text = text, deleteTemp = deleteTemp)

    def do_exclude(self, filename):
        """ Marks the indicated filename as not to be included.  The
        filename may include shell globbing characters, and may or may
        not include a dirname.  (If it does not include a dirname, it
        refers to any file with the given basename from any
        directory.)"""

        if not self.currentPackage:
            raise OutsideOfPackageError

        self.currentPackage.excludeFile(filename)

    def do_dir(self, dirname, newDir = None, unprocessed = None):

        """ Adds the indicated directory hierarchy to the current
        package.  The directory hierarchy is walked recursively, and
        all files that match a known extension are added to the package.

        newDir specifies the directory name within the package which
        the contents of the named directory should be installed to.
        If it is omitted, the contents of the named directory are
        installed to the root of the package.

        If unprocessed is false (the default), bam files are loaded and
        scanned for textures, and these texture paths within the bam
        files are manipulated to point to the new paths within the
        package.  If unprocessed is true, this operation is bypassed,
        and bam files are packed exactly as they are.
        """

        if not self.currentPackage:
            raise OutsideOfPackageError

        dirname = Filename(dirname)
        if not newDir:
            newDir = ''

        self.__recurseDir(dirname, newDir, unprocessed = unprocessed)

    def __recurseDir(self, filename, newName, unprocessed = None):
        dirList = vfs.scanDirectory(filename)
        if dirList:
            # It's a directory name.  Recurse.
            prefix = newName
            if prefix and prefix[-1] != '/':
                prefix += '/'
            for subfile in dirList:
                filename = subfile.getFilename()
                self.__recurseDir(filename, prefix + filename.getBasename())
            return

        # It's a file name.  Add it.
        ext = filename.getExtension()
        if ext == 'py':
            self.currentPackage.addFile(filename, newName = newName,
                                        explicit = False, unprocessed = unprocessed)
        else:
            if ext == 'pz':
                # Strip off an implicit .pz extension.
                newFilename = Filename(filename)
                newFilename.setExtension('')
                newFilename = Filename(newFilename.cStr())
                ext = newFilename.getExtension()

            if ext in self.knownExtensions:
                self.currentPackage.addFile(filename, newName = newName,
                                            explicit = False, unprocessed = unprocessed)


    def readContentsFile(self):
        """ Reads the contents.xml file at the beginning of
        processing. """

        self.contents = {}
        self.contentsChanged = False

        contentsFilename = Filename(self.installDir, 'contents.xml')
        doc = TiXmlDocument(contentsFilename.toOsSpecific())
        if not doc.LoadFile():
            # Couldn't read file.
            return

        xcontents = doc.FirstChildElement('contents')
        if xcontents:
            if self.hostDescriptiveName is None:
                self.hostDescriptiveName = xcontents.Attribute('descriptive_name')
            
            xpackage = xcontents.FirstChildElement('package')
            while xpackage:
                pe = self.PackageEntry()
                pe.loadXml(xpackage)
                self.contents[pe.getKey()] = pe
                xpackage = xpackage.NextSiblingElement('package')

    def writeContentsFile(self):
        """ Rewrites the contents.xml file at the end of
        processing. """

        if not self.contentsChanged:
            # No need to rewrite.
            return

        contentsFilename = Filename(self.installDir, 'contents.xml')
        doc = TiXmlDocument(contentsFilename.toOsSpecific())
        decl = TiXmlDeclaration("1.0", "utf-8", "")
        doc.InsertEndChild(decl)

        xcontents = TiXmlElement('contents')
        if self.hostDescriptiveName:
            xcontents.SetAttribute('descriptive_name', self.hostDescriptiveName)

        contents = self.contents.items()
        contents.sort()
        for key, pe in contents:
            xpackage = pe.makeXml()
            xcontents.InsertEndChild(xpackage)

        doc.InsertEndChild(xcontents)
        doc.SaveFile()
        

# The following class and function definitions represent a few sneaky
# Python tricks to allow the pdef syntax to contain the pseudo-Python
# code they do.  These tricks bind the function and class definitions
# into a bit table as they are parsed from the pdef file, so we can
# walk through that table later and perform the operations requested
# in order.

class metaclass_def(type):
    """ A metaclass is invoked by Python when the class definition is
    read, for instance to define a child class.  By defining a
    metaclass for class_p3d and class_package, we can get a callback
    when we encounter "class foo(p3d)" in the pdef file.  The callback
    actually happens after all of the code within the class scope has
    been parsed first. """
    
    def __new__(self, name, bases, dict):

        # At the point of the callback, now, "name" is the name of the
        # class we are instantiating, "bases" is the list of parent
        # classes, and "dict" is the class dictionary we have just
        # parsed.

        # If "dict" contains __metaclass__, then we must be parsing
        # class_p3d or class_ppackage, below--skip it.  But if it
        # doesn't contain __metaclass__, then we must be parsing
        # "class foo(p3d)" (or whatever) from the pdef file.
        
        if '__metaclass__' not in dict:
            # Get the context in which this class was created
            # (presumably, the module scope) out of the stack frame.
            frame = sys._getframe(1)
            mdict = frame.f_locals
            lineno = frame.f_lineno

            # Store the class name on a statements list in that
            # context, so we can later resolve the class names in
            # the order they appeared in the file.
            mdict.setdefault('__statements', []).append((lineno, 'class', name, None, None))
            
        return type.__new__(self, name, bases, dict)

class class_p3d:
    __metaclass__ = metaclass_def
    pass

class class_package:
    __metaclass__ = metaclass_def
    pass

class class_solo:
    __metaclass__ = metaclass_def
    pass

class func_closure:

    """ This class is used to create a closure on the function name,
    and also allows the *args, **kw syntax.  In Python, the lambda
    syntax, used with default parameters, is used more often to create
    a closure (that is, a binding of one or more variables into a
    callable object), but that syntax doesn't work with **kw.
    Fortunately, a class method is also a form of a closure, because
    it binds self; and this doesn't have any syntax problems with
    **kw. """

    def __init__(self, name):
        self.name = name

    def generic_func(self, *args, **kw):
        """ This method is bound to all the functions that might be
        called from the pdef file.  It's a special function; when it is
        called, it does nothing but store its name and arguments in the
        caller's local scope, where they can be pulled out later. """

        # Get the context in which this function was called (presumably,
        # the class dictionary) out of the stack frame.
        frame = sys._getframe(1)
        cldict = frame.f_locals
        lineno = frame.f_lineno

        # Store the function on a statements list in that context, so we
        # can later walk through the function calls for each class.
        cldict.setdefault('__statements', []).append((lineno, 'func', self.name, args, kw))