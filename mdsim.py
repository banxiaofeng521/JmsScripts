#!/usr/bin/env python

#LAST MODIFIED: 01-19-09

#ALL public routines of this class are designed to be package-independent,
#i.e., not specific to AMBER.  Specific routines are made private.

#CONVENTION: all calls specifying atom number or residue number start
#            at zero and not one as in the pdb file


Usage = """Runs a md simulation using default settings.

Usage     : mdsim.py [OPTIONS] RUNPATH TIMEINPS PROTEIN

RUNPATH   : path to create for running the simulation      
TIMEINPS  : time to run the simulation, in ps
PROTEIN   : either a pdb file (for initial pdb conformation)
            or a sequence (for initial extended conformation)
OPTIONS   : "--min" to first minimize the initial structure
            "--restrainres=1,8,3,5" to restrain residues 1 to 8
                and 3 to 5
            "--restraindist=7.0" to start all restraints at 7 A
                pairwise distance (7.0 is the default)
            "--skipgzip" to skip gzipping of trajectory files
            "--temp=X" to set temperature setpoint to X
            "--langevin=X" to turn on Langevin dynamics with gamma=X
            "--berendsen=X" to turn on Berendsen coupling with tau=X
            "--andersen=X" to turn on Andersen thermostat with velocity
               rescaling every X time steps
            "--removecom=X" to remove center of mass motion every X steps
            "--shakemode=X" to set shake (1=none, 2=hydrogens, 3=all)
            "--timestep=X" to set timestep to X picoseconds
"""


#check for instructions
import sys
if __name__ == "__main__" and len(sys.argv) < 4:
  print Usage
  sys.exit()

from numpy import *
import os, time, shutil, gzip, copy, random, cPickle, sys, zlib, StringIO, re
import sequence, pdbtools, protein, geometry, scripttools, coords


#======== MAKE AMBER EXCEPTION ========
class SimError(Exception):
  def __init__(self, Msg):
    self.Msg = Msg
  def __str__(self):
    return str(self.Msg)
  

#======== CHECK FOR AMBER EXECUTABLES ========
Paths = os.environ["PATH"].split(":")
FoundSander, FoundTLeap = False, False
for p in Paths:
  FoundTLeap = os.path.isfile(os.path.join(p, "tleap"))
  if FoundTLeap: break
if not FoundTLeap:
  print "Cound not find tleap."
for p in Paths:
  FoundSander = os.path.isfile(os.path.join(p, "sander"))
  if FoundSander: break
if not FoundSander:
  print "Cound not find sander."


#======== DEFINE A FILE WRAPPER FOR WRITE FUNCTIONS ========

class myFile:
  def __init__(self, FileName, Mode, UseGzip = False, Auto = True):
    """Wrapper for file objects that switches seamlessly between gzipping
and normal files.  The option Auto has the following behavior.  For reads,
it first tries to find FileName. If FileName can't be found and it ends
with '.gz', it will look for a similar unzipped file without the '.gz'.
For writes, it will determine whether or not to gzip based on the extension.
"""
    #automatically check for gzipping
    FileName = FileName.strip()
    if Auto:
      if "r" in Mode:
        UseGzip = False
        if FileName.endswith(".gz"):       
          UseGzip = True
          if not os.path.isfile(FileName):
            FileName = FileName[:-3]
      elif "w" in Mode or "a" in Mode:
        UseGzip = FileName.endswith(".gz")
    #check for binary if gzipping
    if UseGzip and not "b" in Mode: Mode = Mode + "b"
    #open the files
    self.fobj = file(FileName, Mode)
    self.UseGzip = UseGzip
    if self.UseGzip:
      self.f = gzip.GzipFile(filename = os.path.basename(FileName), fileobj = self.fobj)
    else:
      self.f = self.fobj
  def read(self, size = -1):
    return self.f.read(size)
  def readline(self, size = -1):
    return self.f.readline(size)
  def write(self, s):
    self.f.write(s)
  def __del__(self):
    self.close()
  def close(self):
    if self.UseGzip:
      self.f.close()
      self.f = self.fobj
    self.f.close()
  def seek(self, i):
    self.f.seek(i)
  def tell(self):
    return self.f.tell()
   
#helper function
def IsList(l):
  "Returns true if x is a list or array; needed to handle both lists and numpy arrays."
  return not type(l) is str and "__getitem__" in dir(l)


#======== TEMPLATES AND DEFAULTS FOR CLASS DATA ========

#Below is the template string for tleap with tokens in all caps.
#Following that is a dictionary of default (initial) values for
#the tokens.
TLeapTmpl = """source leaprc.[FORCEFIELD]
set default PBradii [PBRADII]
[PRE]
[CMDS]
[BONDS]
[POST]
check sys
saveAmberParm sys prmtop.parm7 tleapout.crd
savepdb sys tleapout.pdb
quit
"""

TLeapDflts = {
    "FORCEFIELD":"ff99SB",
    "PBRADII":"mbondi2",
    "PRE":"",
    "CMDS":"",
    "BONDS":"",
    "POST":"" }


#Below are template strings for sander with tokens in all caps.
#Following that is a dictionary of default (initial) values for
#the tokens.  Templates are for MD and minimization.  New tokens
#can easily be added by modifying both the template and adding
#a new variable to the defaults dictionary.
RunTmplMD = """trajectory segment
 &cntrl
  imin = 0, nstlim = [STEPSMD], ntwr = [STEPSMD],
  ntx = [INPUTMODE], irest = [RESTARTVEL], 
  igb = [IGB], gbsa = [GBSA],
  cut = 16.0, surften = [SURFACETENSION],
  tempi = [TEMPSET], ntt = [TEMPMODE], temp0 = [TEMPSET],
  tautp = [BERENDSENTAU], vrand = [STEPSTEMP],
  gamma_ln = [LANGEVINGAMMA], vlimit = [VELOCITYLIMIT],
  ntc = [SHAKEMODE], ntf = [SHAKEMODE], tol = 1.0d-8,
  dt = [STEPSIZE], nrespa = 2,
  ntb = 0, iwrap = 0, nscm = [STEPSREMOVECOM],
  ntpr = [STEPSMD], ntave = 0,
  ioutfm = 0, ntwx = [STEPSSAVE], ntwe = [STEPSSAVE],
  ig = [SEED],
  nmropt = 1,
  ntr = [POSRESTON],
[POSRESTOPT]
 &end
[WEIGHTSOPT]
 &wt type='END'  &end
 DISANG=restraints.txt
END
END
"""

RunTmplMin = """Minimization - steepest descent followed by conj grad
 &cntrl 
  imin = 1, maxcyc = [STEPSMINTOT], ncyc = [STEPSMINSD],
  ntx = 1, irest = 0,
  igb = [IGB], gbsa = [GBSA],
  cut = 999.0, surften = [SURFACETENSION],
  ntc = 1, ntf = 1, tol = 1.0d-8,
  ntb = 0, iwrap = 0,
  ntpr=100, ntwr=1000,
  nmropt = 1,
  ntr = [POSRESTON],
[POSRESTOPT]
 &end
[WEIGHTSOPT]
 &wt type='END'  &end
 DISANG=restraints.txt
END
END
"""


#Temperature weight change template
TempWeightTmpl = """ &wt type='TEMP0', istep1=[STEPSWEIGHT1], istep2=[STEPSWEIGHT2],
     value1=[TEMPSET1], value2=[TEMPSET2],  &end"""

#Templates for the weight-change information
WeightTmpl = {
 "RESTSCALE" : """ &wt type='REST', istep1=[STEPSWEIGHT1], istep2=[STEPSWEIGHT2],
     value1=[RESTSCALE1], value2=[RESTSCALE2],  &end""",
 "NONRESTSCALE" : """ &wt type='ALL', istep1=[STEPSWEIGHT1], istep2=[STEPSWEIGHT2],
     value1=[NONRESTSCALE1], value2=[NONRESTSCALE2],  &end""",
 "RADIUSSCALE" : """ &wt type='RSTAR', istep1=[STEPSWEIGHT1], istep2=[STEPSWEIGHT2],
     value1=[RADIUSSCALE1], value2=[RADIUSSCALE2],  &end""",
 "ELECSCALE" : """ &wt type='ELEC', istep1=[STEPSWEIGHT1], istep2=[STEPSWEIGHT2],
     value1=[ELECSCALE1], value2=[ELECSCALE2],  &end""",
 "NONBONDSCALE" : """ &wt type='NB', istep1=[STEPSWEIGHT1], istep2=[STEPSWEIGHT2],
     value1=[NONBONDSCALE1], value2=[NONBONDSCALE2],  &end"""
 }


#note that STEPSREMOVECOM will be automatically changed to STEPSMD + 1 so that
#amber doesn't remove these things if RemoveCOM = True
RunDflts = { "STEPSMD":500, "STEPSMINTOT":250, "STEPSMINSD":200, "STEPSMINCG":50,
             "STEPSSAVE":500, "STEPSREMOVECOM":500,
             "VELOCITYLIMIT":0.,
             "TEMPSET":270.0, "TEMPSET1":270., "TEMPSET2":270., "STEPSTEMP":500,
             "TEMPMODE":0, "BERENDSENTAU":1.0, "LANGEVINGAMMA":0.0,
             "SEED":314159, "STEPSIZE":0.002,
             "WEIGHTSOPT":"", "STEPSWEIGHT1":0, "STEPSWEIGHT2":500,
             "RESTSCALE1":1.0, "RESTSCALE2":1.0,
             "NONRESTSCALE1":1.0, "NONRESTSCALE2":1.0,
             "RADIUSSCALE1":1.0, "RADIUSSCALE2":1.0,
             "ELECSCALE1":1.0, "ELECSCALE2":1.0,
             "NONBONDSCALE1":1.0, "NONBONDSCALE2":1.0,
             "POSRESTON":0, "POSRESTOPT":"", "POSRESTFCONST":0.01,
             "SHAKEMODE":2, "SURFACETENSION":0.005,
             "INPUTMODE":1, "RESTARTVEL":0,
             "IGB":8, "GBSA":1}


#Below is the template string for the NMR restraints file.
#There are versions for single atom as well as groups.
#Following that is a dictionary of default (initial) values for
#the tokens.  New tokens can easily be added by modifying both
#the template and adding a new variable to the defaults dictionary.
RestTmplAtom = """#[RESTLABEL]
 &rst
  iat=  [ATOM1], [ATOM2], [ATOM3], [ATOM4],
  iresid=0,
  r1=[DIST1], r2=[DIST2], r3=[DIST3], r4=[DIST4],
  rk2=[FCONST2], rk3=[FCONST3], ialtd=[RESTTYPE],
 &end"""

RestTmplGroup = """#[RESTLABEL]
 &rst
  iat=  -1,  -1, 0, 0,
  iresid=0,
  r1= [DIST1], r2= [DIST2], r3=[DIST3], r4=[DIST4],
  rk2=[FCONST2], rk3=[FCONST3], ir6=0, ialtd=[RESTTYPE],
  igr1=  [ATOM1],
  igr2=  [ATOM2],
 &end"""

RestDflts = {"DIST1":1.30, "DIST2":1.80, "DIST3":6.50, "DIST4":7.00,
    "FCONST2":0.50, "FCONST3":0.50, "RESTLABEL":"restraint",
    "ATOM1":0, "ATOM2":0, "ATOM3":-1, "ATOM4":-1, "RESTTYPE":0,
    "POSREST":False}

#Variables for ion-ion repulsion to remove salt bridges.  Note that
#this is not copied into SimClass objects but remains a global var.
RestIonVars = {"DIST1":4.0, "DIST2":6.0, "DIST3":10., "DIST4":10.,
    "FCONST2":0.5, "FCONST3":0.0}

#Below is the template string for the GROUP specification which
#can be used for the making positional (Cartesian) restraints.
#The tokens are replaced by tokens given in the normal restraints
#dictionary.
PosRestTmpl = """  restraint_wt=[FCONST],
  restraintmask='[GROUP]',"""
PosRestDflts = {"POSREST":True, "FCONST":0.01, "ATOMS":[],
                "REFPOS":zeros((0,3), float), "GROUP":"[@*]"}

#Below is the dictionary of data collected from a sander run.
#The suffixes '1' and '2' indicate variable values at the start
#and end of the run.
DataDflts = {"ETOT1":0., "EPOT1":0., "EKIN1":0., 
             "EREST1":0., "TEMP1":0., "PRES1":0.,
             "EVDW1":0., "EEL1":0., "ESURF1":0.,
             "EBOND1":0., "EANG1":0., "EDIH1":0.,
             "ERESTBOND1":0., "ERESTANG1":0., "ERESTDIH1":0.,
             "ETOT2":0., "EPOT2":0., "EKIN2":0., 
             "EREST2":0., "TEMP2":0., "PRES2":0.,
             "EVDW2":0., "EEL2":0., "ESURF2":0.,
             "EBOND2":0., "EANG2":0., "EDIH2":0.,
             "ERESTBOND2":0., "ERESTANG2":0., "ERESTDIH2":0.,
             "ETOTAVG":0., "EPOTAVG":0., "EKINAVG":0., 
             "ERESTAVG":0., "TEMPAVG":0., "PRESAVG":0.,
             "EVDWAVG":0., "EELAVG":0., "ESURFAVG":0.,
             "EBONDAVG":0., "EANGAVG":0., "EDIHAVG":0.,
             "ERESTBONDAVG":0., "ERESTANGAVG":0., "ERESTDIHAVG":0.,
             "TIME1":0., "TIME2":0.}

#Below details a fix described by Simmerling and coworkers for
#salt bridge interactions that scales N+ bound hydrogen
#intrinsic radii to something smaller, given by this variable.
PBRadiiFixHRad = 1.1
#Geney, Layten, Gomperts, Hornak & Simmerling JCTC 2, 115 (2006)
PBRadiiGLGHS = {"*": {"H1":1.1, "H2":1.1, "H3":1.1},  #n-terminus
                "HIP": {"HD1":1.1, "HE2":1.1},
                "ARG": {"HH11":1.1, "HH12":1.1, "HH21":1.1, "HH22":1.1},
                "LYS": {"HZ1":1.1, "HZ2":1.1, "HZ3":1.1} }
#Kim, Jang, Pak, JCP 127, 145104 (2007)
PBRadiiKJP = {"*": {"H1":1.105, "H2":1.105, "H3":1.105, #n-terminus
                    "OXT":1.275}, #c-terminus
              "HIP": {"HD1":1.105, "HE2":1.105},
              "ARG": {"HH11":1.105, "HH12":1.105, "HH21":1.105, "HH22":1.105, "NH1":1.318, "NH2":1.318},
              "LYS": {"HZ1":1.105, "HZ2":1.105, "HZ3":1.105, "NZ":1.318},
              "GLU": {"CD":1.445, "OE1":1.275, "OE2":1.275},
              "ASP": {"CG":1.445, "OD1":1.275, "OD2":1.275},
              "SER": {"OG":1.320},
              "THR": {"OG1":1.320} }
#this one will be automatically applied; use {} for none
PBRadiiAuto = PBRadiiGLGHS


#======== GLOBAL CLASS VARIABLES ========

#Boltzmann's constant (kcal/mol)
kB = 0.0019858775

#Below are strings used to execute the amber programs.
#The MinRef and MDRef commands are used with the positional
#restraints since a reference structure (ref.crd) is needed.
TLeapCmd = "tleap -f tleapin.txt > tleapout.txt"
AmbPdbCmd = "(ambpdb -p prmtop.parm7 < current.crd > current.pdb) >& /dev/null" #"current.crd | ambpdb -p prmtop.parm7 > current.pdb 2> /dev/null"
SanderMinCmd = "sander -O -i minin.txt -o minout.txt -p prmtop.parm7 -c minin.crd -r minout.crd -e minene.txt -inf mininfo.txt"
SanderMDCmd = "sander -O -i mdin.txt -o mdout.txt -p prmtop.parm7 -c mdin.crd -r mdout.crd -x mdtrj.crd -e mdene.txt -inf mdinfo.txt"
SanderRefCmdOpt = " -ref ref.crd"


#VARIABLES BELOW PROVIDE INFORMATION FOR PARSING AMBER OUTPUT:
#Gives a list of strings to look for at the start of a block in the mdout
#file.  Any of the tokens in RunVars will be replaced (i.e., STEPSMD).
#Each variable will be appended with the second number of the block, so '1' for
#NSTEP = 0, and '2' for the first NSTEP = STEPSMD.
MDOutBlockStart = [("NSTEP=0","1"),    ("NSTEP=[STEPSMD]","2"),
                   ("NSTEP=[STEPSMD]","AVG"), ("NSTEP=[STEPSMD]","RMS")]
#This provides where to stop in the output block.
MDOutBlockStop = "===================="
#This dictionary tells which elements of a split string correspond to the
#data variables.  The labels give the lowercased, no-spaces name of the
#variable before an equal sign in the amber mdout format.
#More than one element will be summed.
MDOutParseData = {"ETOT":("etot",), "EPOT":("eptot",), "EKIN":("ektot",),
                  "EREST":("restraint",), "TEMP":("temp(k)",),
                  "PRES":("press",), "EVDW":("vdwaals", "1-4nb"),
                  "EBOND":("bond",), "EANG":("angle",), "EDIH":("dihed",),
                  "EEL":("eelec", "1-4eel", "egb"), "ESURF":("esurf",),
                  "TIME":("time(ps)",),"ERESTBOND":("bond-2",),
                  "ERESTANG":("angle-2",), "ERESTDIH":("torsion",)} 
#This dictionary tells which elements of a split string correspond to the
#data variables in the ene file.  The tuples provide the index of the element(s).
#More than one element indicates that they should be summed.  Negative elements
#indicate that they should be subtracted.  
EneParseData = {"ETOT":(3,), "EPOT":(32,), "EKIN":(4,), "EREST":(43,), "TEMP":(6,),
                "PRES":(21,), "EVDW":(33,41), "EBOND":(37,), "EANG":(38,), "EDIH":(39,),
                "EEL":(34,36,42), "ESURF":(32,-43,-33,-41,-37,-38,-39,-34,-36,-42),
                "TIME":(2,)}
#This gives the length of the ene header in bytes.
EneHeadLen = 737
#This gives the length of the ene block in bytes.
EneBlockLen = 750
#This gives the number of lines in the ene header.
EneHeadLines = 10
#This gives the number of lines in the ene block.
EneBlockLines = 10
#This gives the number of header lines in the trj file.
TrjHeadLines = 1

#These are all the files which must be saved prior to an undo.
DataFiles = ["mdout.txt","mdout.crd","mdtrj.crd","mdene.txt","current.crd"]

#These are all files which are copied to just start a run
PrepFiles = ["prmtop.parm7", "current.crd", "ref.crd"]

#These are all files relevant to the current state of the system
CurrentFiles = ["current.crd"]

#List of files produced by doing an MD run or a min run or tleap
MinFiles = ["minout.crd", "minout.txt", "mininfo.txt"]
MDFiles = ["mdout.crd", "mdout.txt", "mdinfo.txt", "sanderout.txt",
           "mdene.txt", "mdtrj.crd"]
TLeapFiles = ["tleapout.pdb", "tleapout.txt", "tleapout.crd", "leap.log"]

#all files
AllFiles = TLeapFiles + MinFiles + MDFiles + PrepFiles

#These are all the elements of the class which can be considered "Data" and
#which can be saved for restart purposes.  NOT included are the path variables
#and large position and velocity arrays.
ClassData = ["TLeapVars", "RunVars", "RestVars", "RestList",  
             "TimeStart", "TimeStop",
             "Data", "UndoData", "Seq", "Atoms", "AtomRes", "UserData",
             "ChangedMD", "ChangedMin", "ChangedRest", "AutoRecenter",
             "MDRecenter", "LinkWeightSteps"]

#This specifies all class elements which are data specific to the current
#configuration.  So, for example, if the configuration is swapped between
#two class instances, these data elements should be as well.  Sequence
#data is also included in case one is swapping different sequences as well.
ConfigData = ["Data", "UndoData", "Pos", "Vel", "Seq", "Atoms", "AtomRes"]

#These specify the caps used if one indicates that a sequence should
#be capped.
NCap = "ACE"
CCap = "NME"

#Whether or not to explicitly terminate the chain correctly if there are no caps;
#AMBER will usually modify the residues if this is False anyways
ChargedTermini = True

#Maximum random number seed for amber.
MaxSanderSeed = 71276

#Whether or not to store the full path rather than relative--
#seems that using relative path can cause some problems
UseFullPath = True

#True will stop when a major simulation error is detected, such as
#tleap or sander not returning a coordinate set.
StopOnError = True

#True will print mdout.txt or minout.txt if stopped on an error.
PrintOnError = True


#======== ACCESSORY ROUTINES ========

def ReplaceD(s, d, Bracket = True):
  """Replaces tokens in a string using a dictionary.  By default,
this function will convert floats to %.3f format for Amber.
* s: string whose tokens will be replaced
* d: dictionary of tokens (string keys) and their replacements
     (values, may be of any type)
* MaxLen: maximum length (in characters) of replacement values.
* Bracket: tokens are bracketed in string ala '[TOKEN]'"""
  for (k, v) in d.iteritems():
    if Bracket: k = '[' + k + ']'
    if type(v) is float:
      r = "%f" % v
    elif IsList(v):
      r = ",".join([str(x) for x in v[:200]])
    else:
      r = str(v)
    s = s.replace(k, r)
  return s

def CleanAmberPdb(PdbFile):
  "Formats an Amber-produced pdb to standard residue names, etc."
  if not os.path.isfile(PdbFile): return
  Pdb = file(PdbFile, "r").read()
  OutPdb = []
  for l in Pdb.split("\n"):
    if l.startswith("ATOM"):
      if l[17:20] in ["HID","HIE","HIP"]:
        l = l[:17] + "HIS" + l[20:]
      if l[17:20] in ["CYX"]:
        l = l[:17] + "CYS" + l[20:]
    OutPdb.append(l)
  OutPdb = "\n".join(OutPdb)
  file(PdbFile, "w").write(OutPdb)

def IsAtom(x, Atom):
  "Indicates whether or not an atom name matches Atom, after removing numbers."
  for n in range(0,9): x = x.replace(str(n), "")
  x = x.strip()
  return x.startswith(Atom)  
  

#======== CLASS ROUTINES ========

class SimClass:
  def __init__(self, RunPath = "."):
    """Initializes a simulation class wrapper for Amber.
* RunPath: path to store all amber files (default is current)"""
    self.Seq = []       #text labels of sequence
    self.Atoms = []     #text labels of atoms
    self.AtomRes = []   #number of residue to which atoms belong
    if UseFullPath:
      self.RunPath = os.path.abspath(RunPath)
    else:
      self.RunPath = RunPath
    #make the path if it doesn't exist
    if not os.path.isdir(self.RunPath):
      try:
        os.mkdir(self.RunPath)
      except OSError:
        print "Error making SimClass path."
    #initialize class variables from defaults
    self.TLeapVars = copy.deepcopy(TLeapDflts)
    #note: RunVars should never be modified directly,
    #      but rather passed through SimClass[""]
    self.RunVars = copy.deepcopy(RunDflts)
    #note: RestVars should never be modified directly,
    #      but rather passed through SimClass[""]
    self.RestVars = copy.deepcopy(RestDflts)
    self.Data = copy.deepcopy(DataDflts)
    self.UndoData = copy.deepcopy(DataDflts)
    #set the restraint list to nothing
    self.RestList = []
    #set the timers to initial values
    self.TimeStart = time.time()
    self.TimeStop = self.TimeStart
    #initialize the user data dictionary (user can add anything)
    self.UserData = {}
    #initialize the changed variables
    self.ChangedMin = True
    self.ChangedMD = True
    self.ChangedRest = True
    #link weight steps to number of md/min steps
    self.LinkWeightSteps = True
    #recenter after MD and min? (automatically disabled if positional restraints on)
    self.AutoRecenter = True
    #let amber recenter?
    self.MDRecenter = True
    #position and velocity holders
    self.Pos, self.Vel = None, None
    #bonds list
    self.Bonds = []
    #whether or not velocities are contained in current.crd
    self.HasVel = False


  #SPECIAL FUNCTIONS

  def __setattr__(self, key, val):
    """Sets a class attribute."""
    self.__dict__[key] = val
    if key == "RestList":
      self.ChangedRest = True

  def __getitem__(self, key):
    """Allows user to get variables using dictionary notation."""
    if self.Data.has_key(key): return self.Data[key]
    if self.RunVars.has_key(key): return self.RunVars[key]
    if self.RestVars.has_key(key): return self.RestVars[key]
    if self.UserData.has_key(key): return self.UserData[key]
    raise KeyError, "Key %s not found." % key

  def __setitem__(self, key, val):
    """Allows user to set variables using dictionary notation.
If user key does not exist, it will be added to the
list of user variables."""
    if self.RunVars.has_key(key):
      #check if the same value
      if val == self.RunVars[key]: return
      self.RunVars[key] = val
      #check minimization
      if key in ["STEPSMINSD","STEPSMINCG"]:
        self.RunVars["STEPSMINTOT"] = self.RunVars["STEPSMINSD"] + self.RunVars["STEPSMINCG"]
      #check temp
      if key == "TEMPSET":
        self.RunVars["TEMPSET1"] = val
        self.RunVars["TEMPSET2"] = val
      elif key == "TEMPSET1":
        self.RunVars["TEMPSET"] = val
      #check velocities restart
      if key == "RESTARTVEL":
        if val == 0:
          self.RunVars["INPUTMODE"] = 1
        else:
          self.RunVars["INPUTMODE"] = 5
      self.SetChangedRun()
    elif self.RestVars.has_key(key):
      #check if the same value
      if val == self.RestVars[key]: return
      self.RestVars[key] = val
      self.ChangedRest = True
    else:
      self.UserData[key] = val

  def __delitem__(self, key):
    """Allows user to use del to remove a user variable."""
    if self.UserData.has_key(key):
      del self.UserData[key]
    else:
      print "Key not found."

  def __contains__(self, key):
    """Indicates presence of a variables."""
    return self.Data.has_key(key) or self.RunVars.has_key(key) or \
           self.RestVars.has_key(key) or self.UserData.has_key(key)
    

  #PROPERTY RETRIEVAL

  def __UpdateWeights(self, NSteps):
    """Updates the weights section."""
    s = ""
    for (k, v) in WeightTmpl.items():
      k1, k2 = k + "1", k + "2"
      if not (self.RunVars[k1] == 1 and self.RunVars[k2] == 1):
        if self.LinkWeightSteps:
          t = ReplaceD(v, {"STEPSWEIGHT1":0, "STEPSWEIGHT2":NSteps})
        else:
          t = v
        s += ReplaceD(t, self.RunVars) + "\n"
    #update temperature
    if not self.RunVars["TEMPSET1"] == self.RunVars["TEMPSET2"]:
      if self.LinkWeightSteps:
        t = ReplaceD(TempWeightTmpl, {"STEPSWEIGHT1":0, "STEPSWEIGHT2":NSteps})
      else:
        t = TempWeightTmpl
      s += ReplaceD(t, self.RunVars) + "\n"
    self.RunVars["WEIGHTSOPT"] = s

  def SetChangedRun(self):
    "Tells the class some settings have changed."
    self.ChangedMin = True
    self.ChangedMD = True

  def ShowAllData(self):
    "Shows all data in the class."
    print "RunVars= ", self.RunVars, "\n"
    print "RestVars= ", self.RestVars, "\n"
    print "Data= ", self.Data, "\n"
    print "UserData= ", self.UserData, "\n"

  def ElapsedTime(self):
    """Shows the elapsed time in seconds for the last MD
or minimization run."""
    return self.TimeStop-self.TimeStart

  def StorageSize(self):
    "Reports the current size of files in the run path."
    if os.path.exists(self.RunPath):
      return sum([os.path.getsize(os.path.join(self.RunPath,f))
                  for f in os.listdir(self.RunPath)])
    else:
      return 0


  #VALUE SETTERS (generic interface to the outside world)

  def SetTemp(self, Temp):
    """Sets the temperature.
* Temp: float with temperature value"""
    self["TEMPSET"] = Temp

  def SetSeed(self, Seed):
    """Sets the random seed.
* Seed: integer not to exceed MaxSanderSeed;
        -1 will select one automatically"""
    self["SEED"] = Seed

  def SetMDSteps(self, NSteps):
    """Sets the number of molecular dynamics steps.
* NSteps: integer with number of time steps"""    
    self["STEPSMD"] = NSteps

  def SetMinSteps(self, NSteps1, NSteps2):
    """Sets the number of minimization steps.
* NSteps1: number of steepest descent iterations
* NSteps2: number of conjugate gradient iterations"""
    self["STEPSMINSD"] = NSteps1
    self["STEPSMINCG"] = NSteps2

  def SetStepSize(self, StepSize):
    """Sets the simulation timestep.  
* StepSize: float with timestep in picoseconds"""  
    self["STEPSIZE"] = StepSize

  def SetPreBuildString(self, BuildString):
    """Sets a package-specific string for system building.
Here, it is a string run by tleap before the system is created.
* SetupString: string"""
    self.TLeapVars["PRE"] = BuildString
    self.SetChangedRun()

  def SetPostBuildString(self, BuildString):
    """Sets a package-specific string for system building.
Here, it is a string run by tleap after the system is created.
* SetupString: string"""
    self.TLeapVars["POST"] = BuildString
    self.SetChangedRun()


  #MANAGING CLASS DATA

  def __UpdateVer(self):
    "Updates to the latest version of SimClass."
    for (k, v) in RunDflts.items():
      if not k in self.RunVars: self.RunVars[k] = v
    for (k, v) in RestDflts.items():
      if not k in self.RestVars: self.RestVars[k] = v
    for (k, v) in TLeapDflts.items():
      if not k in self.TLeapVars: self.TLeapVars[k] = v
    #convert restraints
    if type(self.RestList) is dict:
      self.RestList = self.RestList.values()
      
  def GetPdb(self):
    "Returns a string of pdb data for the current configuration."
    fn = os.path.join(self.RunPath, "current.pdb")
    if os.path.isfile(fn):
      return file(fn,"r").read()
    else:
      return ""

  def WritePdb(self, FileName):
    "Writes the current configuration to a pdb file."
    Pdb = self.GetPdb()
    file(FileName, "w").write(Pdb)

  def GetProteinClass(self):
    "Returns a ProteinClass object for the current configuration."
    fn = os.path.join(self.RunPath, "current.pdb")
    if os.path.isfile(fn):
      p = protein.ProteinClass(Pdb = fn)
      #update with current.crd
      p.Pos = self.GetPos()
      return p
    else:
      return None
    
  def Save(self):
    "Saves all class information in a backup file called simclass.dat.gz"
    fn = os.path.join(self.RunPath, "simclass.dat.gz")
    #write in memory first, then save to file, to max write speed
    s = StringIO.StringIO()
    cPickle.dump(self, gzip.GzipFile("simclass.dat.gz", "w", fileobj = s),
                 cPickle.HIGHEST_PROTOCOL)
    s = s.getvalue()
    file(fn, "wb").write(s)

  def Load(self):
    "Loads all class information from a backup file called simclass.dat.gz"
    fn = os.path.join(self.RunPath, "simclass.dat.gz")
    f = myFile(fn, "r")
    try:
      self = cPickle.load(f)
    except IOError:
      print "IOError reading from simclass.dat"
    f.close()
    self.__UpdateVer()
      
  def SaveData(self):
    """Saves just simulation data to a backup file called sim.dat.gz
    (i.e., does not include path variables.)."""
    fn = os.path.join(self.RunPath, "sim.dat.gz")
    #write in memory first, then save to file, to max write speed
    s = StringIO.StringIO()
    cPickle.dump([(itm, self.__dict__[itm]) for itm in ClassData],
                 gzip.GzipFile("sim.dat.gz", "w", fileobj = s),
                 cPickle.HIGHEST_PROTOCOL)
    s = s.getvalue()
    file(fn, "wb").write(s)

  def LoadData(self):
    "Loads just simulation data from a backup file called sim.dat.gz"
    fn = os.path.join(self.RunPath, "sim.dat.gz")
    f = myFile(fn, "r")
    alldata = cPickle.load(f)
    for itm, dat in alldata:
      self.__dict__[itm] = dat
    f.close()
    self.__UpdateVer()

  def DumpsData(self):
    "Returns a pickled string of simulation data."
    return zlib.compress(cPickle.dumps([(itm, self.__dict__[itm])
                                        for itm in ClassData],
                                       cPickle.HIGHEST_PROTOCOL))

  def LoadsData(self, s):
    """Loads a pickled string of simulation data.
* s: string of pickled data (derived from DumpsData)"""
    alldata = cPickle.loads(zlib.decompress(s))
    for itm, dat in alldata:
      self.__dict__[itm] = dat
    self.__UpdateVer()

  def CopyData(self, Source):
    """Copies the simulation data from another sim class.
* Source: another SimClass object"""
    for itm in ClassData:
      self.__dict__[itm] = copy.deepcopy(source.__dict__[itm])

  def MovePath(self, RunPath, PrepOnly = False, CurrentOnly = False):
    """Moves necessary files from one path to another.
* RunPath: new path for mdsim files
* PrepOnly: only copy the files needed to start a run from current state
* CurrentOnly: only copy the current configuration
"""
    if PrepOnly:
      FileList = PrepFiles
    elif CurrentOnly:
      FileList = CurrentFiles
    else:
      FileList = AllFiles
    #copy files
    for f in FileList:
      oldf = os.path.join(self.RunPath, f)
      newf = os.path.join(RunPath, f)
      if os.path.isfile(oldf): shutil.copy(oldf, newf)
    #set the new run path
    if UseFullPath:
      self.RunPath = os.path.abspath(RunPath)
    else:
      self.RunPath = RunPath
    self.ChangedRest = True
    self.SetChangedRun()
    #update the velocity holders
    self.HasVel = False
      

  #MANAGING CURRENT CONFIG

  def __CurrentUpdate(self, FileName):
    """Updates the currend.crd file with FileName"""
    #must be run from within RunPath
    if os.path.isfile(FileName):
      if os.path.getsize(FileName) == 0:
        print "mdsim.CurrentUpdate: %s is zero-length in path %s" % (FileName, os.getcwd())
        return False
      else:
        shutil.copy(FileName,"current.crd")
        if self["POSRESTON"] == 0 and self.AutoRecenter and len(self.Atoms) > 0: self.Recenter()
        os.system(AmbPdbCmd)
        CleanAmberPdb("current.pdb")
        return True
    else:
      print "mdsim.CurrentUpdate: Cannot find file %s in path %s" % (FileName, os.getcwd())
      return False

  def __CurrentCopy(self, FileName):
    """Copies the current.crd file to FileName"""  
    #must be run from within RunPath
    if os.path.isfile("current.crd"):
      if os.path.getsize("current.crd") == 0:
        print "mdsim.CurrentUpdate: current.crd is zero-length in path %s" % (os.getcwd(),)
        return False
      else:
        shutil.copy("current.crd", FileName)
        return True
    else:
      print "mdsim.CurrentCopy: Cannot find file current.crd in path %s" % (os.getcwd(),)
      return False
      
  def UndoPrep(self):
    "Sets up an undo point."
    self.UndoData.update(self.Data)
    for f in DataFiles:
      fn = os.path.join(self.RunPath, f).strip()
      if os.path.isfile(fn):
        shutil.copy(fn, fn+".und")

  def UndoRun(self):
    "Returns to the last undo point."
    self.Data.update(self.UndoData)
    for f in DataFiles:
      fn = os.path.join(self.RunPath, f).strip()
      if os.path.isfile(fn+".und"):
        if os.path.isfile(fn): os.remove(fn)
        os.rename(fn+".und", fn)
    #update the velocity holders
    self.HasVel = False


  #======== POSITION GETTING AND SETTING ========

  def GetPos(self, UseVel = False):
    """Returns arrays of current atomic positions (and optionally velocities)."""
    CurFile = os.path.join(self.RunPath, "current.crd")
    if not os.path.isfile(CurFile):
      raise SimError, "Cannot find current.crd"
    else:
      #read file
      vals = file(CurFile, "r").read()
      #parse into a n by 3 array
      #remove first two lines
      i = vals.find("\n")
      i = vals.find("\n", i+1)
      vals = vals[i:].replace("\n","")
      try:
        vals = [float(vals[i:i+12]) for i in range(0, len(vals), 12)]
      except ValueError:
        raise SimError, "Could not parse current.crd"
      Pos = array(vals, float).reshape((-1,3))
      N = len(self.Atoms)
      Pos, Vel = Pos[:N,:], Pos[N:,:]
      if UseVel:
        return Pos, Vel
      else:
        return Pos
   
  def SetPos(self, Pos, Vel = None):
    """Sets the atomic positions and optionally velocities."""
    def FmtArray(a):
      """Formats an array for Amber input.  Returns string."""
      p = ["%12.7f" % x for x in a.flat]
      r = mod(len(p), 6)
      if r > 0: p.extend([""]*(6 - r))
      s = "\n".join(["".join(p[i:i+6]) for i in range(0, len(p), 6)])
      return s
    CurFile = os.path.join(self.RunPath, "current.crd")
    if not os.path.isfile(CurFile):
      raise SimError, "Cannot find current.crd"
    elif not Pos.shape == (len(self.Atoms), 3):
      raise SimError, "Position array is dimensionality %s; should be %s" % (repr(Pos.shape), (len(self.Atoms), 3))
    else:
      s = "ACE".ljust(80) + "\n"
      s += "%5d  0.0000000E+00\n" % len(Pos)
      s += FmtArray(Pos) + "\n"
      if not Vel is None and len(Vel) > 0:
        if Vel.shape == (len(self.Atoms), 3):
          s += FmtArray(Vel) + "\n"
        else:
          raise SimError, "Velocity array is not correct dimensionality: %s vs %s" % (repr(Vel.shape), repr(Pos.shape))
      file(CurFile, "w").write(s)
      if Vel is None or len(Vel) == 0:
        self.HasVel = False

  def ScaleVel(self, Factor):
    """Scales all velocities by a factor."""
    Pos, Vel = self.GetPos(Vel = True)
    Vel = Vel * Factor
    self.SetPos(Pos, Vel)

  def LoadPos(self, UseVel = False):
    """Loads positions into self.Pos (and optionally velocities)."""
    if UseVel:
      self.Pos, self.Vel = self.GetPos(UseVel = True)
    else:
      self.Pos = self.GetPos(UseVel = False)

  def ClearPos(self):
    """Clears loaded positions and velocities."""
    self.Pos, self.Vel = None, None

  def SavePos(self):
    """Saves self.Pos and, if present, self.Vel to current configuration."""
    if self.Pos is None:
      raise IndexError, "self.Pos is not loaded (currently None)."
    else:
      self.SetPos(self.Pos, self.Vel)

  def GetRefPos(self):
    """Returns an array of reference atomic positions."""
    CurFile = os.path.join(self.RunPath, "ref.crd")
    if not os.path.isfile(CurFile):
      raise SimError, "Cannot find ref.crd"
    else:
      #read file
      vals = file(CurFile, "r").read()
      #parse into a n by 3 array
      #remove first two lines
      i = vals.find("\n")
      i = vals.find("\n", i+1)
      vals = vals[i:].replace("\n","")
      try:
        vals = [float(vals[i:i+12]) for i in range(0, len(vals), 12)]
      except ValueError:
        raise SimError, "Could not parse ref.crd"
      Pos = array(vals, float).reshape((-1,3))
      Pos = Pos[:len(self.Atoms),:]
      return Pos

  def SetRefPos(self, Pos):
    """Sets the atomic positions for the reference file."""
    CurFile = os.path.join(self.RunPath, "ref.crd")
    if not os.path.isfile(CurFile):
      raise SimError, "Cannot find ref.crd"
    elif not Pos.shape == (len(self.Atoms), 3):
      raise SimError, "Position array is not correct dimensionality: " + repr(Pos.shape)
    else:
      NPerLine = 6
      Fmt = "%12.7f"
      s = "ACE".ljust(80) + "\n"
      s += "%5d  0.0000000E+00\n" % len(Pos)
      p = [Fmt % x for x in Pos.flat]
      r = mod(len(p), NPerLine)
      if r > 0: p.extend([""]*(NPerLine - r))
      for i in xrange(0, len(p), NPerLine):
        s += "".join(p[i:i + NPerLine]) + "\n"
      file(CurFile, "w").write(s)      

  def Recenter(self):
    """Centers the current configuration at the origin;
issues a warning if any Cartesian positional restraints are on."""
    CurFile = os.path.join(self.RunPath, "current.crd")
    if not os.path.isfile(CurFile): return
    if self["POSRESTON"] == 1:
      #issue warning
      print "mdsim.Recenter: positional restraints are on."
    Pos, Vel = self.GetPos(UseVel = True)
    Pos = Pos - mean(Pos, axis=0)
    self.SetPos(Pos, Vel)

        
  #MODEL BUILDING      
    
  def SysInitSeq(self, Seq, Cap = False, CapN = False, CapC = False):
    """Sets the system to be built from specified sequence.
* Seq: string of sequence characters, either 1 letter continuous
       or 3 letter code with spaces
* Cap: True or False for whether to cap with N and C terminal
       residues"""
    if Cap: CapN, CapC = True, True
    s = sequence.SeqToList(Seq)
    if CapN: s = [NCap] + s
    if CapC: s = s + [CCap]
    if ChargedTermini:
      #check for the n-terminal residue
      aa = sequence.AAInst(s[0])
      if not aa is None:
        if not aa.Cap and not len(s[0]) > 3:
          if s[0][0].islower():
            s[0] = "n" + s[0]
          else:
            s[0] = "N" + s[0]
      #check for the c-terminal residue
      aa = sequence.AAInst(s[-1])
      if not aa is None:
        if not aa.Cap and not len(s[-1]) > 3:
          if s[-1][0].islower():
            s[-1] = "c" + s[-1]
          else:
            s[-1] = "C" + s[-1]
    self.Seq = s
    s = sequence.SeqToAA3(s)
    self.TLeapVars["CMDS"] = "sys = sequence{" + s + "}"
    self.SetChangedRun()

  def SysInitPdb(self, PdbFile):
    """Sets the system to be built from a PDB file.
* PdbFile: string name of the pdb file to be imported"""
    FullPdbFile = os.path.abspath(PdbFile)
    if not os.path.isfile(FullPdbFile):
      raise IOError, "Cannot find pdb file."
    self.TLeapVars["CMDS"] = "sys = loadpdb " + FullPdbFile
    self.Seq = []
    self.SetChangedRun()

  def SysInitPdbUsingSeq(self, PdbFile, Seq, Cap = False,
                         CapN = False, CapC = False):
    """Sets the system to be built from a PDB file, but
with a specified sequence which overrides the file seq.
* PdbFile: string name of the pdb file to be imported
* Seq: string of sequence characters, either 1 letter continuous
       or 3 letter code with spaces
* Cap: True or False for whether to add caps to the sequence
       (but not the pdb, which must have caps already)"""
    FullPdbFile = os.path.abspath(PdbFile)
    if Cap: CapN, CapC = True, True
    s = sequence.SeqToList(Seq)
    if CapN: s = [NCap] + s
    if CapC: s = s + [CCap]
    if ChargedTermini:
      #check for the n-terminal residue
      aa = sequence.AAInst(s[0])
      if not aa is None:
        if not aa.Cap and not len(s[0]) > 3:
          if s[0][0].islower():
            s[0] = "n" + s[0]
          else:
            s[0] = "N" + s[0]
      #check for the c-terminal residue
      aa = sequence.AAInst(s[-1])
      if not aa is None:
        if not aa.Cap and not len(s[-1]) > 3:
          if s[-1][0].islower():
            s[-1] = "c" + s[-1]
          else:
            s[-1] = "C" + s[-1]
    self.Seq = s
    s = "seq = {" + sequence.SeqToAA3(s) + "}\n"
    s+= "sys = loadPdbUsingSeq " + FullPdbFile + " seq"
    self.TLeapVars["CMDS"] = s
    self.SetChangedRun()

  def SysScaleCharges(self, Factor = 1.):
    "Sets the charges in the system to be scaled by a factor."
    self.TLeapVars["POST"] = "scalecharges sys %.3f" % Factor
    self.SetChangedRun()

  def SysAddBond(self, ResNum1, AtomName1, ResNum2, AtomName2):
    "Adds a bond between the specified atoms."
    self.Bonds.append((ResNum1, AtomName1, ResNum2, AtomName2))
    self.SetChangedRun()

  def SysDelBond(self, ResNum1, AtomName1, ResNum2, AtomName2):
    "Deletes a bond between the specified atoms."
    b = (ResNum1, AtomName1, ResNum2, AtomName2)
    if b in self.Bonds:
      self.Bonds = [x for x in self.Bonds if not x == b]
    else:
      raise ValueError, "Bond %d.%s sys.%d.%s not found" % (ResNum1, AtomName1, ResNum2, AtomName2)
    self.SetChangedRun()
  
  def SysBuild(self, SetupFile = "", PrmtopFile = None, RstFile = None):
    """Builds the system based on prior settings (e.g., SysInitSeq).
Optionally a setup file can be read in for use with TLeap and
should contain commands to create a unit called sys.  A preamble
loading the force field and a prologue saving the unit sys
will automatically be added.  Or Prmtop and Rst files can be
supplied from a previous tleap output."""
    #see if we should load a setup file
    if len(SetupFile) > 0:
      if os.path.isfile(SetupFile):
        f = open(SetupFile, "r")
        self.TLeapVars["CMDS"] = f.read()
        f.close()

    #make the path if needed    
    if not os.path.isdir(self.RunPath): os.mkdir(self.RunPath)

    #delete old files
    for f in TLeapFiles:
      fn = os.path.join(self.RunPath, f)
      if os.path.isfile(fn): os.remove(fn)

    #check for user supplied tleap files
    if PrmtopFile is None or RstFile is None:

      #check for disulfide bonds
      if len(self.Seq) > 0:
        olds = sequence.SeqToAA3(self.Seq)
        for (ResNum1, AtomName1, ResNum2, AtomName2) in self.Bonds:
          if AtomName1 == "SG" and AtomName2 == "SG" \
             and sequence.AreAliases(self.Seq[ResNum1], "CYS") \
             and sequence.AreAliases(self.Seq[ResNum2], "CYS"):
            self.Seq[ResNum1] = self.Seq[ResNum1].replace("S", "X")
            self.Seq[ResNum2] = self.Seq[ResNum2].replace("S", "X")
        s = sequence.SeqToAA3(self.Seq)
        self.TLeapVars["CMDS"] = self.TLeapVars["CMDS"].replace(olds, s)
            
      #add bonds text
      self.TLeapVars["BONDS"] = ""
      for (ResNum1, AtomName1, ResNum2, AtomName2) in self.Bonds:
        s = "bond sys.%d.%s sys.%d.%s\n" % (ResNum1+1, AtomName1, ResNum2+1, AtomName2)
        self.TLeapVars["BONDS"] = self.TLeapVars["BONDS"] + s

      #fill in variable values
      s = ReplaceD(TLeapTmpl, self.TLeapVars)
      
      #write tleap in file
      TLeapInFile = open(os.path.join(self.RunPath, "tleapin.txt"), "w")
      TLeapInFile.write(s)
      TLeapInFile.close()

      #run tleap
      cwd = os.getcwd()
      os.chdir(self.RunPath)
      os.system(TLeapCmd)
      
      #replace the current.crd file with tleap output
      if not self.__CurrentUpdate("tleapout.crd") and StopOnError:
        os.chdir(cwd)
        raise SimError, "Could not find tleap output."

      #get pdb file info
      pdb = file("tleapout.pdb", "r").read()
      #get a list of residue numbers
      self.AtomRes = [int(line[22:26])-1 for line in pdb.split("\n") if line.startswith("ATOM")]
      #get a list of atom types
      self.Atoms = [line[12:16].upper() for line in pdb.split("\n") if line.startswith("ATOM")]
      #get the sequence
      self.Seq = []
      for line in pdb.split("\n"):
        if line.startswith("ATOM") and int(line[22:26]) > len(self.Seq): 
          self.Seq.append(line[17:20].strip())   

      #change the path back
      os.chdir(cwd)      

    else:

      shutil.copy(PrmtopFile, os.path.join(self.RunPath, "prmtop.parm7"))
      shutil.copy(RstFile, os.path.join(self.RunPath, "current.crd"))
      #get a list of residue numbers
      self.AtomRes = coords.GetPrmtopAtomRes(PrmtopFile)
      #get a list of atom types
      self.Atoms = coords.GetPrmtopAtomNames(PrmtopFile)
      self.Atoms = coords.AmbToPdbAtomNames(self.Atoms)
      #get the sequence
      self.Seq = coords.GetPrmtopSeq(PrmtopFile)      

    self.SetChangedRun()

    #update the velocity holders
    self.HasVel = False

    #update pb radii
    self.ModifyPBRadii(PBRadiiAuto)


  #REPARAM PBRADII

  def FixPBRadii(self):
    """Scales radii of N+ bound hydrogen to a smaller value,
to reduce salt bridge formation according to the protocol
of Simmerling, et al.  This should be run after SysBuild()."""
    fn = os.path.join(self.RunPath, "prmtop.parm7")
    Prmtop = file(fn, "rU").read()
    #find the radius section
    s1 = re.search(r"%FLAG RADII *\n%FORMAT\(5E16\.8\) *\n(.*)\n%FLAG", Prmtop, re.DOTALL).group(1)
    #convert to numbers
    try:
      Radii = [float(x) for x in s1.split()]
    except ValueError:
      raise ValueError, "Could not read prmtop radii."
    #get the hydrogens bonded to N salt atoms
    p = protein.ProteinClass(Pdb = os.path.join(self.RunPath, "current.pdb"))
    Bonds = p.Bonds()
    Hydrogens = p.AtomInd(Element = "H")
    Cations = p.AtomInd(AtomCharged = True, ChargeSign = 1, Element = "N",
                        UsePartialCharges = False)
    HInd = [a for (a,b) in Bonds if b in Cations and a in Hydrogens] + \
           [b for (a,b) in Bonds if a in Cations and b in Hydrogens]
    #modify the radii
    for i in HInd:
      Radii[i] = PBRadiiFixHRad
    #create a new string
    s2 = ["%16.8e" %  x for x in Radii]
    s2 = ["".join(s2[i:i+5]) for i in range(0, len(s2), 5)]
    s2 = "\n".join(s2)
    #replace the old one
    Prmtop = Prmtop.replace(s1, s2)
    #write the new file
    file(fn, "w").write(Prmtop)

  def ModifyPBRadii(self, RadiiDict):
    """Changes radii of atoms with names in RadiiDict.
This should be run after SysBuild()."""
    fn = os.path.join(self.RunPath, "prmtop.parm7")
    Prmtop = file(fn, "rU").read()
    #find the atom names
    s1 = re.search(r"%FLAG ATOM_NAME *\n%FORMAT\(20a4\) *\n(.*?)\n%FLAG", Prmtop, re.DOTALL).group(1)
    s1 = s1.replace("\n","")
    #find the radius section
    s2 = re.search(r"%FLAG RADII *\n%FORMAT\(5E16\.8\) *\n(.*?)\n%FLAG", Prmtop, re.DOTALL).group(1)
    s2orig = s2
    s2 = s2.replace("\n","")
    #find the atoms corresponding to residues
    s3 = re.search(r"%FLAG RESIDUE_POINTER *\n%FORMAT\(10I8\) *\n(.*?)\n%FLAG", Prmtop, re.DOTALL).group(1)
    s3 = s3.replace("\n","")
    #find the residue names
    s4 = re.search(r"%FLAG RESIDUE_LABEL *\n%FORMAT\(20a4\) *\n(.*?)\n%FLAG", Prmtop, re.DOTALL).group(1)
    s4 = s4.replace("\n","")
    #split
    try:
      AtomNames = [s1[x:x+4].strip() for x in range(0,len(s1),4)]
      Radii = [float(s2[x:x+16]) for x in range(0,len(s2),16)]
      Seq = [s4[x:x+4].strip() for x in range(0,len(s4),4)]
      NAtom = len(AtomNames)
      ResPointer = [int(s3[x:x+8]) - 1 for x in range(0,len(s3),8)] + [NAtom]
      AtomRes = []
      for i in range(len(ResPointer) - 1):
        a, b = ResPointer[i], ResPointer[i+1]
        for j in range(a,b):
          AtomRes.append(Seq[i])
    except ValueError:
      raise ValueError, "Could not read prmtop names / radii."
    #convert terminal "O" to "OXT"
    for i in range(len(ResPointer) - 1):
      a, b = ResPointer[i], ResPointer[i+1]
      if "OXT" in AtomNames[a:b]:
        for j in range(a,b):
          if AtomNames[j] == "O": AtomNames[j] = "OXT"
    #change the radii
    for (i,(rn,an)) in enumerate(zip(AtomRes, AtomNames)):
      if "*" in RadiiDict:
        Radii[i] = RadiiDict["*"].get(an, Radii[i])
      if rn in RadiiDict:
        Radii[i] = RadiiDict[rn].get(an, Radii[i])
    #create a new radii string
    s = ["%16.8e" %  x for x in Radii]
    s = ["".join(s[i:i+5]) for i in range(0, len(s), 5)]
    s = "\n".join(s)
    #replace the old one
    Prmtop = Prmtop.replace(s2orig, s)
    #write the new file
    file(fn, "w").write(Prmtop)    

    
  #RESTRAINTS

  def RestClear(self):
    "Clears all current NMR and Cartesian anchoring restraints."
    self["POSRESTOPT"] = ""
    self["POSRESTON"] = 0
    self.RestList = []
    self.ChangedRest = True

  def RestSetAtoms(self, Atom1, Atom2, Atom3 = None, Atom4 = None,
                   Strength = 1.0, FConst2 = None, FConst3 = None,
                   Dist1 = None, Dist2 = None, Dist3 = None, Dist4 = None):
    """Sets a NMR restraint between two atoms.
* Atom1: list or single number of first atom (starts at zero)
* Atom2: list or single number of second atom (starts at zero)
* Atom3: number of third atom (for angle or torsional restraint)
* Atom4: number of fourth atom (for torsional restraint)
* Strength: float coefficient to multiply force constants;
  default is 1.0"""
    RestVars = copy.deepcopy(self.RestVars)
    #replace default variables
    if Atom3 is None:
      RestVars["RESTLABEL"] = "Atom-atom"
    elif Atom4 is None:
      RestVars["RESTLABEL"] = "Angle"
    else:
      RestVars["RESTLABEL"] = "Torsion"
    if not FConst2 is None: RestVars["FCONST2"] = FConst2
    if not FConst3 is None: RestVars["FCONST3"] = FConst3
    RestVars["FCONST2"] = Strength * RestVars["FCONST2"]
    RestVars["FCONST3"] = Strength * RestVars["FCONST3"]
    if RestVars["FCONST2"] == 0 and RestVars["FCONST3"] == 0: return
    if not Dist1 is None: RestVars["DIST1"] = Dist1
    if not Dist2 is None: RestVars["DIST2"] = Dist2
    if not Dist3 is None: RestVars["DIST3"] = Dist3
    if not Dist4 is None: RestVars["DIST4"] = Dist4
    if IsList(Atom1):
      RestVars["ATOM1"] = [x + 1 for x in Atom1]
      if len(RestVars["ATOM1"]) == 1:
        RestVars["ATOM1"] = RestVars["ATOM1"][0]
    else:
      RestVars["ATOM1"] = Atom1 + 1
    if IsList(Atom2):
      RestVars["ATOM2"] = [x + 1 for x in Atom2]
      if len(RestVars["ATOM2"]) == 1:
        RestVars["ATOM2"] = RestVars["ATOM2"][0]
    else:
      RestVars["ATOM2"] = Atom2 + 1
    if not Atom3 is None: RestVars["ATOM3"] = Atom3 + 1
    if not Atom4 is None: RestVars["ATOM4"] = Atom4 + 1
    self.RestList.append(RestVars)
    self.ChangedRest = True

  def __GetAtomNum(self, Res, AtomName):
    """Gets the number of an atom, given a residue number and name.
Selects the first matching atom in the given residue,
and will automatically change CB to CA for glycine.
* Res: integer with residue number (starts at zero)
* AtomName: string with atom type name"""
    AtomName = AtomName.upper().strip()
    ResName = self.Seq[Res].upper().strip()
    #check for CB and glycine
    if ResName == "GLY" and AtomName == "CB": AtomName = "CA"
    #return atom num
    for (i, Atom) in enumerate(self.Atoms):
      if Atom.upper().strip() == AtomName and self.AtomRes[i] == Res:
        return i
    print [Atom for (i, Atom) in enumerate(self.Atoms) if self.AtomRes[i]==Res]
    print "Could not find %s in residue %d %s" % (AtomName, Res, self.Seq[Res])
    return -1

  def RestSetRes(self, Res1, Res2, AtomName1 = None, AtomName2 = None,
                 Strength = 1.0, FConst2 = None, FConst3 = None,
                 Dist1 = None, Dist2 = None, Dist3 = None, Dist4 = None):
    """Sets a restraint between two residues using specified atoms.
Selects the first matching atom in the given residue,
and will automatically change CB to CA for glycine.
* Res1: number of first residue
* Res2: number of second residue
* AtomName1: string name of atom type in first residue;
  If nothing is specified, residue centroid is used
* AtomName2: string name of atom type in second residue;
  If nothing is specified, residue centroid is used
* Strength: float coefficient to multiply force constants;
  default is 1.0"""
    RestVars = copy.deepcopy(self.RestVars)
    #replace default variables
    RestVars["RESLABEL"] = "Residue-residue"
    if not FConst2 is None: RestVars["FCONST2"] = FConst2
    if not FConst3 is None: RestVars["FCONST3"] = FConst3
    RestVars["FCONST2"] = Strength * RestVars["FCONST2"]
    RestVars["FCONST3"] = Strength * RestVars["FCONST3"]
    if RestVars["FCONST2"] == 0 and RestVars["FCONST3"] == 0: return
    if not Dist1 is None: RestVars["DIST1"] = Dist1
    if not Dist2 is None: RestVars["DIST2"] = Dist2
    if not Dist3 is None: RestVars["DIST3"] = Dist3
    if not Dist4 is None: RestVars["DIST4"] = Dist4
    if AtomName1 is None or AtomName1 == "*" or AtomName1.lower() == "residue":
      RestVars["ATOM1"] = [i+1 for (i,Atom) in enumerate(self.Atoms)
        if self.AtomRes[i] == Res1 and not IsAtom(Atom, "H")]
      if len(RestVars["ATOM1"]) == 1:
        RestVars["ATOM1"] = RestVars["ATOM1"][0]
    else:
      RestVars["ATOM1"] = self.__GetAtomNum(Res1, AtomName1) + 1
    if AtomName2 is None or AtomName2 == "*" or AtomName2.lower() == "residue":
      RestVars["ATOM2"] = [i+1 for (i,Atom) in enumerate(self.Atoms)
        if self.AtomRes[i] == Res2 and not IsAtom(Atom, "H")]
      if len(RestVars["ATOM2"]) == 1:
        RestVars["ATOM2"] = RestVars["ATOM2"][0]
    else:
      RestVars["ATOM2"] = self.__GetAtomNum(Res2, AtomName1) + 1
    self.RestList.append(RestVars)
    self.ChangedRest = True

  def RestAddIonRepulsion(self, Strength = 1.0, FConst2 = None, FConst3 = None,
    Dist1 = None, Dist2 = None, Dist3 = None, Dist4 = None):
    """Adds a repulsive restraint between oppositely-charged ions."""
    #make a list of salt atoms using sequence templates
    PosSaltAtoms, NegSaltAtoms = [], []
    for (i, Atom) in enumerate(self.Atoms):
      Atom = Atom.strip()
      Res = self.Seq[self.AtomRes[i]]
      AA = sequence.AAInst(Res)
      if AA is None: continue
      if Atom in AA.PosAtoms:
        PosSaltAtoms.append(i)
      elif Atom in AA.NegAtoms:
        NegSaltAtoms.append(i)
      else:
        continue
    #look for backbone N with three H
    for i in range(len(self.Seq)):
      l1 = [j for j in range(len(self.Atoms)) if self.AtomRes[j] == i]
      l2 = [self.Atoms[j].strip() for j in l1]
      if "H1" in l2 and "H2" in l2 and "H3" in l2 and "N" in l2:
        PosSaltAtoms.append(l1[l2.index("N")])
    #look for backbone O with OXT
    for i in range(len(self.Seq)):
      l1 = [j for j in range(len(self.Atoms)) if self.AtomRes[j] == i]
      l2 = [self.Atoms[j].strip() for j in l1]
      if "OXT" in l2 and "O" in l2:
        NegSaltAtoms.append(l1[l2.index("OXT")])
        NegSaltAtoms.append(l1[l2.index("O")])
    #remove duplicates
    PosSaltAtoms = [x for (i,x) in enumerate(PosSaltAtoms) if not x in PosSaltAtoms[i+1:]]
    NegSaltAtoms = [x for (i,x) in enumerate(NegSaltAtoms) if not x in NegSaltAtoms[i+1:]]
    #get the variables
    RestVars = copy.deepcopy(self.RestVars)
    RestVars.update(RestIonVars)
    #replace default variables
    if not FConst2 is None: RestVars["FCONST2"] = FConst2
    if not FConst3 is None: RestVars["FCONST3"] = FConst3
    if not Dist1 is None: RestVars["DIST1"] = Dist1
    if not Dist2 is None: RestVars["DIST2"] = Dist2
    if not Dist3 is None: RestVars["DIST3"] = Dist3
    if not Dist4 is None: RestVars["DIST4"] = Dist4 
    #adjust the strength as necessary
    RestVars["FCONST2"] = Strength * RestVars["FCONST2"]
    RestVars["FCONST3"] = Strength * RestVars["FCONST3"]
    if RestVars["FCONST2"] == 0 and RestVars["FCONST3"] == 0: return
    for i in PosSaltAtoms:
      for j in NegSaltAtoms:
        ThisRestVars = copy.deepcopy(RestVars)
        ThisRestVars["ATOM1"], ThisRestVars["ATOM2"] = i+1, j+1
        ThisRestVars["RESTLABEL"] = "Ion repulsion" 
        self.RestList.append(ThisRestVars)
    self.ChangedRest = True
    
  def RestSetPhiPsi(self, ResNum, Phi, Psi, PhiTol, PsiTol, FConst):
    """Adds a phi-psi restraint to a residue."""
    Res = self.Seq[ResNum]
    IndN = self.__GetAtomNum(ResNum, "N")
    IndCA = self.__GetAtomNum(ResNum, "CA")
    IndC = self.__GetAtomNum(ResNum, "C")
    NTerm = sequence.NTerminal(Res) or sequence.NCap(Res)
    CTerm = sequence.CTerminal(Res) or sequence.CCap(Res)
    if -1 in [IndN, IndCA, IndC]: return
    if not ResNum <= 0 and not Res in protein.FixedPhi and not NTerm:
      IndC0 = self.__GetAtomNum(ResNum - 1, "C")
      if IndC0 >= 0:
        self.RestSetAtoms(IndC0, IndN, IndCA, IndC,
                          FConst2 = FConst, FConst3 = FConst,
                          Dist1 = Phi - 179., Dist2 = Phi - PhiTol,
                          Dist3 = Phi + PhiTol, Dist4 = Phi + 179.)
    if not ResNum >= len(self.Seq) - 1 and not Res in protein.FixedPsi and not CTerm:
      IndN2 = self.__GetAtomNum(ResNum + 1, "N")
      if IndN2 >= 0:
        self.RestSetAtoms(IndN, IndCA, IndC, IndN2,
                          FConst2 = FConst, FConst3 = FConst,
                          Dist1 = Psi - 179., Dist2 = Psi - PsiTol,
                          Dist3 = Psi + PsiTol, Dist4 = Psi + 179.)  
 

  def RestDistAll(self):
    "Returns all current restraint distances."
    Pos = self.GetPos(UseVel = None)
    return [RestDist(Pos, r) for r in self.RestList]

  def RestEnergyAll(self):
    "Returns all current restraint energies."
    Pos = self.GetPos(UseVel = False)
    return [RestEnergy(RestDist(Pos, r), r) for r in self.RestList]



#======== POSITIONAL CARTESIAN SPACE RESTRAINTS

  def __CondenseList(self, NumList, Total):
    """Condenses a list of numbers into a namelist string like 1-5,7,8-10,20"""
    #check for all included
    if NumList == range(1, Total+1): return "*"
    #collapse list
    s = ""
    Last = None
    Count = 0
    for x in NumList:
      if Last is None:
        s += str(x)
      elif not x == Last + 1:
        if Count > 0: s += "-" + str(Last)
        s += "," + str(x)
        Count = 0
      else:
        Count += 1
      Last = x
    if Count > 0: s += "-" + str(Last)
    return s

  def __ExpandList(self, NumStr):
    """Expands a string of numbers like 1-5,7,8-10,20"""
    l = []
    for itm in NumStr.split(","):
      if "-" in itm:
        a, b = [int(x) for x in itm.split("-")]
        l.extend(range(a,b+1))
      else:
        l.append(int(itm))
    return l

  def __GetResSelectStr(self, NumList, AtomSelectStr = ""):
    """Returns a selection string for NumList.  Will automatically
pick out the negated version if it is shorter."""
    N = len(self.Seq)
    NegNumList = [x for x in range(N) if not x in NumList]
    NumList = [x+1 for x in NumList]
    NegNumList = [x+1 for x in NegNumList]
    if len(AtomSelectStr) > 0:
      s1 = ":%s%s" % (self.__CondenseList(NumList, N), AtomSelectStr)
      s2 = "(!:%s & %s)" % (self.__CondenseList(NegNumList, N), AtomSelectStr)
    else:
      s1 = ":%s" % self.__CondenseList(NumList, N)
      s2 = "!:%s" % self.__CondenseList(NegNumList, N)
    if len(s2) < len(s1):
      return s2
    else:
      return s1

  def __GetAtomSelectStr(self, NumList):
    """Returns a selection string for NumList.  Will automatically
pick out the negated version if it is shorter."""
    N = len(self.Atoms)
    NegNumList = [x for x in range(N) if not x in NumList]
    NumList = [x+1 for x in NumList]
    NegNumList = [x+1 for x in NegNumList]
    s1 = "@%s" % self.__CondenseList(NumList, N)
    s2 = "!@%s" % self.__CondenseList(NegNumList, N)
    if len(s2) < len(s1):
      return s2
    else:
      return s1    

  def PosRestSetRes(self, AAResList = [], BBResList = [],
                    FConst = None, Strength = 1.0):
    """Sets a harmonic restraint to a location in Cartesian space
using residues in RestList.  The default force constant for all
such restraints is specified by the class variable POSRESTCONST.
* AAResList: list of residue numbers for all atom rest (numbers begin at 0)
* BBResList: list of residue numbers for just backbone restraints
NOTE: either list may be the string "*" for all residues
"""
    if len(AAResList) == 0 and len(BBResList) == 0: AAResList = "*"
    Atoms = []
    if AAResList == "*":
      GroupStr = ":*"
      Atoms = range(len(self.Atoms))
    elif len(AAResList) == 0:
      if BBResList == "*":
        GroupStr = "@CA,C,N"
        Atoms = [i for (i,a) in enumerate(self.Atoms) if a.strip() in ["CA","C","N"]]
      else:
        GroupStr = self.__GetResSelectStr(BBResList, "@CA,C,N")
        Atoms = [i for (i,a) in enumerate(self.Atoms) if a.strip() in ["CA","C","N"]
                 and self.AtomRes[i] in BBResList]
    else:
      GroupStr = self.__GetResSelectStr(AAResList)
      if BBResList == "*":
        GroupStr += " | @CA,C,N"
        Atoms = [i for (i,a) in enumerate(self.Atoms) if a.strip() in ["CA","C","N"]
                 or self.AtomRes[i] in AAResList]
      else:
        BBResList = [x for x in BBResList if not x in AAResList]
        if len(BBResList) > 0:
          GroupStr += " | " + self.__GetResSelectStr(BBResList, "@CA,C,N")
        Atoms = [i for (i,a) in enumerate(self.Atoms) if (a.strip() in ["CA","C","N"]
                 and self.AtomRes[i] in BBResList) or self.AtomRes[i] in AAResList]
    if len(GroupStr) > 250:
      raise OverflowError, "AMBER positional restraint namelist exceeds 250 characters."
    #make the entry and fill in defaults
    PosRestVars = copy.deepcopy(PosRestDflts)
    PosRestVars["FCONST"] = self.RunVars["POSRESTFCONST"]
    if not FConst is None: PosRestVars["FCONST"] = FConst
    PosRestVars["FCONST"] = PosRestVars["FCONST"] * Strength
    if PosRestVars["FCONST"] == 0: return
    PosRestVars["GROUP"] = GroupStr
    PosRestVars["ATOMS"] = Atoms
    PosRestVars["REFPOS"] = self.GetRefPos().take(Atoms, axis=0)
    #add to the restraint list
    self.RestList.append(PosRestVars)
    self.ChangedRest = True
    
  def PosRestSetAtoms(self, AtomList = None, FConst = None, Strength = 1.0):
    """Sets a harmonic restraint to a location in Cartesian space
using atoms in AtomList.  The default force constant for all
such restraints is specified by the class variable POSRESTCONST."
* AtomList: list of atom numbers (numbers begin at zero)
"""
    if not FConst is None: self["POSRESTFCONST"] = FConst
    if AtomList is None:
      GroupStr = "@*"
      Atoms = range(len(self.Atoms))
    else:
      GroupStr = self.__GetAtomSelectStr(AtomList) 
      if len(GroupStr) > 250:
        raise OverflowError, "AMBER positional restraint namelist exceeds 250 characters."
      Atoms = AtomList
    PosRestVars = copy.deepcopy(PosRestDflts)
    PosRestVars["FCONST"] = self.RunVars["POSRESTFCONST"]
    if not FConst is None: PosRestVars["FCONST"] = FConst
    PosRestVars["FCONST"] = PosRestVars["FCONST"] * Strength
    if PosRestVars["FCONST"] == 0: return
    PosRestVars["GROUP"] = GroupStr
    PosRestVars["ATOMS"] = Atoms
    PosRestVars["REFPOS"] = self.GetRefPos().take(Atoms, axis=0)
    #add to the restraint list
    self.RestList.append(PosRestVars)
    self.ChangedRest = True

  def PosRestClear(self):
    "Clears all Cartesian space anchoring restraints."
    self["POSRESTOPT"] = ""
    self["POSRESTON"] = 0
    self.RestList = [x for x in self.RestList if not IsPosRest(x)]
    self.ChangedRest = True

  def PosRestRefCurrent(self):
    """Sets the current coordinates to be the reference for
Cartesian anchoring restraints."""
    RefFile = os.path.join(self.RunPath, "ref.crd")
    CurFile = os.path.join(self.RunPath, "current.crd")
    if os.path.isfile(CurFile):
      shutil.copy(CurFile, RefFile)
    else:
      raise SimError, "mdsim.PosRestRefCurrent: Cannot find file " + CurFile

  def PosRestRefFile(self, FileName):
    """Uses a reference CRD file for Cartesian anchoring restraints.
* FileName: string name of a CRD file with the reference coordinates"""
    RefFile = os.path.join(self.RunPath, "ref.crd")
    if os.path.isfile(FileName):
      shutil.copy(FileName, RefFile)
    else:
      print "mdsim.PosRestRefFile: Cannot find file " + FileName

  def __UpdateRest(self):
    "Writes the restraint file for sander."
    def IsList(l):
      "Returns true if x is a list or array; needed to handle both lists and numpy arrays."
      return "__getitem__" in dir(l)
    if self.ChangedRest:
      self["POSRESTOPT"] = ""
      self["POSRESTON"] = 0
      s = ""
      for r in self.RestList:
        if IsPosRest(r):
          self["POSRESTOPT"] = ReplaceD(PosRestTmpl, r)
          self["POSRESTON"] = 1
        else:
          GroupSpec = IsList(r["ATOM1"]) or IsList(r["ATOM2"])
          if GroupSpec:
            s += ReplaceD(RestTmplGroup, r) + "\n"
          else:
            s += ReplaceD(RestTmplAtom, r) + "\n"
      file(os.path.join(self.RunPath, "restraints.txt"), "w").write(s)
      self.ChangedRest = False
      
    

  #========RUNNING STUFF========

  def RunMin(self, NSteps1 = -1, NSteps2 = -1):
    """Runs minimization.
* NSteps1: integer number of steepest descent steps;
  defaults to previous value
* NSteps2: integer number of conjugate gradient steps;
  defaults to previous value"""
    #check for steps
    if NSteps2 >= 0:
      self["STEPSMINSD"] = NSteps1
    if NSteps2 >= 0:
      self["STEPSMINCG"] = NSteps2

    #delete old files
    for f in MinFiles:
      fn = os.path.join(self.RunPath, f)
      if os.path.isfile(fn): os.remove(fn)

    #fill in variable values
    self.__UpdateWeights(self["STEPSMINSD"])
    self.__UpdateRest()
   
    if self.ChangedMin:   
      #write sander in file
      s = ReplaceD(RunTmplMin, self.RunVars)
      file(os.path.join(self.RunPath, "minin.txt"), "w").write(s)
      self.ChangedMin = False  

    #run sander
    cwd = os.getcwd()
    os.chdir(self.RunPath)
    self.TimeStart = time.time()
    #copy the current configuration to the min input
    if not self.__CurrentCopy("minin.crd") and StopOnError:
      os.chdir(cwd)
      raise SimError, "Could not create sander minimization input."
    #run the minimization; cmd depends on use of positional restraints
    if self["POSRESTON"]:
      os.system(SanderMinCmd + SanderRefCmdOpt)
    else:
      os.system(SanderMinCmd)
    #copy the min output to the current config
    if not self.__CurrentUpdate("minout.crd") and StopOnError:
      if PrintOnError and os.path.isfile("minout.txt"):
        print "\n========MINOUT.TXT========\n" + file("minout.txt", "r").read() \
              + "\n========MINOUT.TXT========\n"
      os.chdir(cwd)
      raise SimError, "Could not find sander minimization output."
    self.TimeStop = time.time()
    os.chdir(cwd)
    #update the velocity holders
    self.HasVel = False

     
  def RunMD(self, Seed = -1, NSteps = -1, StepSize = 0.):
    """Runs molecular dynamics.
* Seed: random number seed for velocities; default is automatic
  generation
* NSteps: integer number of molecular dynamics steps;
  defaults to previous value
* StepSize: float specifying timestep in picoseconds;
  defaults to previous value"""
    #check for steps and stepsize
    if Seed >= 0:
      self["SEED"] = Seed
    else:
      self["SEED"] = random.randint(0, MaxSanderSeed)
    if NSteps >= 0:
      self["STEPSMD"] = NSteps
    if StepSize > 0.:
      self["STEPSIZE"] = StepSize
    if not self.MDRecenter:
      #turn off amber recentering
      self["STEPSREMOVECOM"] = self["STEPSMD"] + 10

    #check if velocity loading is enabled but there are no velocities
    TurnOnVel = False
    if self["RESTARTVEL"] == 1 and not self.HasVel:
      Pos, Vel = self.GetPos(UseVel = True)
      if len(Vel) == 0:
        self["RESTARTVEL"] = 0
        TurnOnVel = True

    #delete old files
    for f in MDFiles:
      fn = os.path.join(self.RunPath, f)
      if os.path.isfile(fn): os.remove(fn)
      
    #update variables and fill in values
    self.__UpdateWeights(self["STEPSMD"])
    self.__UpdateRest()

    if self.ChangedMD:  
      #write sander in file
      s = ReplaceD(RunTmplMD, self.RunVars)
      file(os.path.join(self.RunPath,"mdin.txt"), "w").write(s)
      self.ChangedMD = False

    #prep for sander
    cwd = os.getcwd()
    os.chdir(self.RunPath)
    self.TimeStart = time.time()
    #copy the current config to the md input
    if not self.__CurrentCopy("mdin.crd") and StopOnError:
      os.chdir(cwd)
      raise SimError, "Could not create sander md input."

    #run sander; cmd depends on use of positional restraints
    if self["POSRESTON"]:
      os.system(SanderMDCmd + SanderRefCmdOpt)
    else:
      os.system(SanderMDCmd)
    #copy the output to the current config
    if not self.__CurrentUpdate("mdout.crd") and StopOnError:
      if PrintOnError and os.path.isfile("mdout.txt"):
        print "\n========MDOUT.TXT========\n" + file("mdout.txt", "r").read() \
              + "\n========MDOUT.TXT========\n"
      os.chdir(cwd)
      raise SimError, "Could not find sander md output."
    self.TimeStop = time.time()
    os.chdir(cwd)
    self.UpdateData()
    
    #update the velocity info holders
    self.HasVel = True
    if TurnOnVel: self["RESTARTVEL"] = 1


  def RunEnergy(self):
    """Updates all energies by running 0 steps in sander."""
    if not self.MDRecenter:
      #turn off amber recentering
      self["STEPSREMOVECOM"] = self["STEPSMINSD"] + 10

    #delete old files
    for f in MDFiles:
      fn = os.path.join(self.RunPath, f)
      if os.path.isfile(fn): os.remove(fn)

    #check if velocity loading is enabled but there are no velocities
    TurnOnVel = False
    if self["RESTARTVEL"] == 1 and not self.HasVel:
      Pos, Vel = self.GetPos(UseVel = True)
      if len(Vel) == 0:
        self["RESTARTVEL"] = 0
        TurnOnVel = True
      
    #update variables and fill in values
    self.__UpdateWeights(1)
    self.__UpdateRest()

    #write sander in file, setting zero steps
    s = ReplaceD(RunTmplMD, {"STEPSMD":0})
    s = ReplaceD(s, self.RunVars)
    file(os.path.join(self.RunPath,"mdin.txt"), "w").write(s)
    self.ChangedMD = True

    #run sander
    cwd = os.getcwd()
    os.chdir(self.RunPath)
    self.TimeStart = time.time()
    #copy the current config to the md input
    if not self.__CurrentCopy("mdin.crd") and StopOnError:
      os.chdir(cwd)
      raise SimError, "Could not create sander md input."
    #run sander; cmd depends on use of positional restraints
    if self["POSRESTON"]:
      os.system(SanderMDCmd + SanderRefCmdOpt)
    else:
      os.system(SanderMDCmd)
    #look for errors
    if not os.path.isfile("mdout.txt") and StopOnError:
      os.chdir(cwd)
      raise SimError, "Could not find sander md output."
    self.TimeStop = time.time()
    os.chdir(cwd)
    self.UpdateData()
    #make all the second suffixes the same
    for k, v in MDOutParseData.iteritems():
      self.Data[k+"2"] = self.Data[k+"1"]
      self.Data[k+"AVG"] = self.Data[k+"1"]
      self.Data[k+"RMS"] = 0.

    #update the velocity params
    self.HasVel = True
    if TurnOnVel: self["RESTARTVEL"] = 1

    
  def UpdateData(self):
    """Gets initial and final simulation data from MD output.
* Steps: False means no steps were run, just to get energies"""
    #read mdout values
    fn = os.path.join(self.RunPath, "mdout.txt")
    if os.path.isfile(fn):
      f = open(fn)
      s = " "
      cont = True
      j = 0
      #search for the first tag
      (tag,suff) = MDOutBlockStart[j]
      tag = ReplaceD(tag, self.RunVars)
      while cont and len(s) > 0:
        s = f.readline()
        if s.replace(" ","").startswith(tag):
          #get block
          t = f.readline()
          while len(t) > 0 and not t.strip().startswith(MDOutBlockStop):
            s += t
            t = f.readline()
          #get tokens and values
          data = {}
          key, nextval = "", False
          #make sure equal marks are spaced for splitting and then split
          s = s.replace("=", " = ")
          for x in s.split():
            if x == "=":
              #next value will be a number
              nextval = True
            elif nextval:
              #we need to add a value to the dictionary;
              #check to see if this key is already there
              if key in data:
                #make a new key
                i = 2
                while "%s-%d" % (key,i) in data: i += 1
                key = "%s-%d" % (key,i)
              #add the value
              try:
                data[key] = float(x)
              except ValueError:
                print "Error on parsing file" + os.path.abspath(fn) + "\n"
              key, nextval = "", False
            else:
              #extend the key
              key = key + x.lower()
          #now parse the data according to the tokens
          for k, v in MDOutParseData.iteritems():
            #add a number to each token name
            lbl = k + suff
            self.Data[lbl] = 0.
            #find the data
            for itm in v:
              self.Data[lbl] += data.get(itm, 0.)
          j += 1
          if j >= len(MDOutBlockStart):
            cont = False
          else:
            #go to next tag
            (tag,suff) = MDOutBlockStart[j]
            tag = ReplaceD(tag, self.RunVars)
      f.close()

  def GetHistory(self, Vars = [v for v in EneParseData.iterkeys()]):
    """Returns a dictionary of variable names (keys) paired with arrays
with values of each variable over the temporal evolution of a
trajectory segment.
* Vars: a list of strings specifying the variable names whose
  history will be returned (default is all)"""
    l = {}
    #make a list of variables
    for k in Vars:
      if EneParseData.has_key(k):
        l[k] = []
    #make sure variables are present
    if len(l) == 0:
      print "Variables not found."
      return
    #open the ene file
    fn = os.path.join(self.RunPath, "mdene.txt")
    if os.path.isfile(fn):
      f = file(fn, "r")
      #skip over header
      for i in range(0,EneHeadLines):
        f.readline()
      while True:
        #read data and get tokens
        s1 = ""
        s2 = ""
        for i in range(0,EneBlockLines):
          s1 = f.readline()
          if len(s1) == 0: break
          s2 += s1
        #see if we reached the end of the file
        if len(s1) == 0: break
        #split into tokens
        data = s2.split()
        #parse each desired token
        for k in l.iterkeys():
          v = 0.
          for toknum in EneParseData[k]:
            if toknum > 0:
              sgn = 1.
            else:
              sgn = -1.
            try:
              v += float(data[abs(toknum)]) * sgn
            except ValueError:
              print "Error on parsing file" + os.path.abspath(fn) + "\n"
          l[k].append(v)
      f.close()
      return l
    

  #CONCATENATION ROUTINES

  def ConcatData(self, Prefix, DataPath, Gzip = True,
    Current = True, Params = True, RaiseErrors = True):
    """Updates current trajectory and energy data in master files.
The following files are updated: prmtop.parm7, current.pdb,
mdtrj.crd, and mdene.txt.
* Prefix: string with the prefix to add to each file it updates
* DataPath: string specifying path location of the master files
* Gzip: Boolean specifying whether to use Gzip (default is True)
* Current: True will update current.pdb
* Params: True will update prmtop.parm7"""
    def TrimLines(s, NLines):
      """Removes first NLines of s"""
      n = 0
      for i in range(NLines):
        n = s.find("\n", n) + 1
        if n <= 0: return ""
      return s[n:]
    def ConcatFile(BaseFile, ConcatFile, NHeadLines):
      """Concatenates File to ConcatFile."""
      if os.path.isfile(BaseFile):     
        s = file(BaseFile, "r").read()
        if len(s) == 0 and RaiseErrors:
          print "File %s is zero-length" % BaseFile
        if os.path.isfile(ConcatFile):
          s = TrimLines(s, NHeadLines)
          myFile(ConcatFile, "a").write(s)
        else:
          myFile(ConcatFile, "w").write(s)
      elif RaiseErrors:
        raise IOError, "Cannot find %s" % BaseFile
    #change to DataPath so gzip names files correctly
    cwd = os.getcwd()
    if UseFullPath:
      rp = self.RunPath
    else:
      rp = os.path.join(cwd, self.RunPath)
    os.chdir(DataPath)
    cpfiles = []
    if Current: cpfiles.append("current.pdb")
    if Params: cpfiles.append("prmtop.parm7")
    for f in cpfiles:
      df = os.path.join(rp,f)
      if os.path.isfile(df):
        shutil.copy(df, Prefix + f)
      elif RaiseErrors:
        raise IOError, "Cannot find %s" % df
    trjFn2 = os.path.join(rp, "mdtrj.crd")
    eneFn2 = os.path.join(rp, "mdene.txt")
    ext = ""
    if Gzip: ext = ".gz"
    trjFn1 = Prefix + "mdtrj.crd" + ext
    eneFn1 = Prefix + "mdene.txt" + ext
    ConcatFile(trjFn2, trjFn1, TrjHeadLines)
    ConcatFile(eneFn2, eneFn1, EneHeadLines)
    os.chdir(cwd)

  def DelConcatData(self, Prefix, DataPath):
    for f in ["prmtop.parm7", "current.pdb", "mdene.txt",
              "mdene.txt.gz", "mdtrj.crd", "mdtrj.crd.gz"]:
      fn = os.path.join(DataPath, Prefix + f)
      if os.path.isfile(fn): os.remove(fn)    

        

#======== FUNCTIONS OPERATING ON CONCAT DATA ========


def GetHistory(DataPath, Prefix = "", Vars = [v for v in EneParseData.iterkeys()],
               NFrameSkip = 0, NFrameRead = -1):
  """Returns a dictionary with variable name, array pairs of
data from concatenated files.
* Prefix: string with the prefix to add to each file it updates
* DataPath: string specifying path location of the master files
* Vars: a list of strings specifying the variable names whose
  history will be returned (default is all)"""
  l = {}
  #make a list of variables
  for k in Vars:
    if EneParseData.has_key(k):
      l[k] = []
  #make sure variables are present
  if len(l) == 0:
    return
  #check for file existence
  fn = os.path.join(DataPath, Prefix + "mdene.txt.gz")
  if not os.path.isfile(fn):
    fn = os.path.join(DataPath, Prefix + "mdene.txt")
  f = myFile(fn, "r")
  #skip over header
  for i in range(0,EneHeadLines):
    f.readline()
  #mark current position
  CurPos = f.tell()
  #get block size
  try:
    BytesPerBlock = 0
    for i in range(EneBlockLines):
      m = len(f.readline())
      if m == 0:
        #could not read first record
        f.close()
        return l
      BytesPerBlock += m
  except IOError:
    f.close()
    return l
  #go back to original position
  f.seek(CurPos)
  #skip as necessary
  try:
    f.seek(CurPos + BytesPerBlock * NFrameSkip)
  except IOError:
    print "Could not skip required number of frames in %s" % os.path.abspath(fn)
    raise
  #read in data
  n = 0
  while n < NFrameRead or NFrameRead < 0:
    n += 1
    #read data and get tokens
    s = f.read(BytesPerBlock)
    if len(s) < BytesPerBlock: break
    #split into tokens
    data = s.split()
    #parse each desired token
    for k in l.iterkeys():
      v = 0.
      for toknum in EneParseData[k]:
        if toknum > 0:
          sgn = 1.
        else:
          sgn = -1.
        try:
          v += float(data[abs(toknum)]) * sgn
        except ValueError:
          print "Error on parsing file" + os.path.abspath(fn) + "\n"
      l[k].append(v)
  f.close()
  if NFrameRead > 0 and not n == NFrameRead:
    raise IOError, "Could not read %d frames from %s" % (NFrameRead, os.path.abspath(fn))
  #return data
  return l

def GetNFrames(DataPath, Prefix = ""):
  """Returns the number of frames in concatenated data.
* Prefix: string with the prefix to add to each file it updates
* DataPath: string specifying path location of the master files"""
  #check for file existence
  fn = os.path.join(DataPath, Prefix + "mdene.txt.gz")
  try:
    f = myFile(fn, "r")
  except IOError:
    f.close()
    return 0
  #skip over header
  for i in range(0,EneHeadLines):
    f.readline()
  #initialize count
  n = 0
  #read first record
  try:
    BytesPerBlock = 0
    for i in range(EneBlockLines):
      m = len(f.readline())
      if m == 0:
        #could not read first record
        f.close()
        return 0
      BytesPerBlock += m
    n = 1
  except IOError:
    f.close()
    return 0
  #now read remaining records  
  while True:
    #read data and get tokens
    try:
      s = f.read(BytesPerBlock)
      if len(s) < BytesPerBlock:
        break
    except IOError:
      break
    n += 1
  f.close()
  #return length
  return n


#======== PREPARING PDB FILES FOR INPUT ========

def PrepPdb(InPdb, OutPdb = None, Seq = None, Cap = False, 
            CapN = False, CapC = False):
  """Reads InPdb and produces OutPdb, which is prepared for input.
* InPdb, OutPdb: string name of the pdb file names to import/create;
                 OutPdb defaults to InPdb
* Seq: optional sequence string
* Cap: True or False for whether to add caps to the sequence
"""
  #check options
  if OutPdb is None: OutPdb = InPdb
  if Cap: CapN, CapC = Cap, Cap
  #make a protein class
  p = protein.ProteinClass(Pdb = InPdb)
  #get sequences and standardize
  if not Seq is None:
    #find alignment
    Map = sequence.SeqMapClass(Seq1 = Seq, Seq2 = p.Seq)
    #trim and add mising residues
    if len(Map) > 0:
      p = p[Map.c:Map.d]
      if Map.a > 0:
        p = protein.ProteinClass(Seq = Seq[:Map.a]) - p
      if Map.b < len(Seq):
        p = p - protein.ProteinClass(Seq = Seq[Map.b:])
    else:
      raise SimError, "No alignment between %s and target sequence" % InPdb
  p = p.Cap(CapN = CapN, CapC = CapC, CapNRes = NCap, CapCRes = CCap) 
  p.Dehydrogen()
  p.WritePdb(OutPdb)


#======== RESTRAINT FUNCTIONS ========

def IsPosRest(Rest):
  """Returns true if a restraint is a positional restraint."""
  return Rest.get("POSREST", False)

def IsSameRest(Rest1, Rest2):
  """Returns true if two restraints are the same."""
  for k, v1 in Rest1.iteritems():
    if not k in Rest2: return False
    v2 = Rest2[k]
    if k == "REFPOS":
      #check positions, using a tolerance
      if not allclose(v1, v2): return False
    else:
      #something else; direct comparison
      if any(v1 != v2): return False
  return True

def RestCompare(Rest1, Rest2):
  """Returns the ratio of Rest2 / Rest1, or zero if they
are not multiplicative of each other."""
  l = []
  for k, v1 in Rest1.iteritems():
    if not k in Rest2: return 0.
    v2 = Rest2[k]
    if k.startswith("FCONST"):
      #check the ratio of force constants
      if v1 == 0 or v2 == 0:
        if not v1 == v2: return 0.
      else:
        l.append(v2 / v1)
    elif k == "REFPOS":
      #check positions, using a tolerance
      if not allclose(v1, v2): return 0.
    elif any(v1 != v2):
      #something else; direct comparison
      return 0.
  if len(l) == 0: return 0.
  l = array(l)
  #check that all force constant ratios are the same
  m = mean(l)
  if any(abs(l - m) > 1.e-8):
    return 0.
  else:
    return m

def CalcRestEnergy(r, Rest):
  "Returns the value of a restraint energy, given a distance and parameters."
  if IsPosRest(Rest):
    return Rest["FCONST"] * r
  else:
    #check defaults
    d1, d2 = Rest["DIST1"], Rest["DIST2"]
    d3, d4 = Rest["DIST3"], Rest["DIST4"]
    k2, k3 = Rest["FCONST2"], Rest["FCONST3"]
    #for torsional or angle restraints, need to convert force constants to ang
    if not Rest["ATOM3"] == -1:
      k2 *= 3.0461742e-4
      k3 *= 3.0461742e-4
      #need to move r to mean of dist2 and dist3
      r = geometry.NearestAngle(r, 0.5*(d2 + d3))
    if Rest["RESTTYPE"] == -1:
      #this is the normal kind of restraint
      if r < d1:
        return k2 * ((d2 - d1)**2 + 2.*(d2 - d1)*(d1 - r))
      elif r >= d1 and r < d2:
        return k2 * (d2 - r)**2
      elif r >= d2 and r < d3:
        return 0.
      elif r >= d3 and r < d4:
        return k3 * (r - d3)**2
      else:
        return k3 * ((d4 - d3)**2 + 2.*(d4 - d3)*(r - d4))
    else:
      #this is the one that flattens out at long distances
      if r < d2:
        return k2 * (d2 - r)**2
      elif r >= d2 and r < d3:
        return 0.
      elif r >= d3 and r < d4:
        return k3 * (r - d3)**2
      else:
        d34 = d4 - d3
        return k3 * (3. - 2. * d34 / (r - d3)) * d34*d34        


def CalcRestDist(Pos, Rest):
  """Returns the distance of a restraint, given a position matrix;
for Cartesian anchoring restraints, returns the sum of distances squared."""
  if IsPosRest(Rest):
    Diff = Pos[Rest["ATOMS"]] - Rest["REFPOS"]
    return sum(Diff**2)
  else:
    #get positions of atoms
    Atom1 = Rest["ATOM1"]
    if not IsList(Atom1): Atom1 = [Atom1]
    Atom1 = [x-1 for x in Atom1]
    Atom2 = Rest["ATOM2"]
    if not IsList(Atom2): Atom2 = [Atom2]
    Atom2 = [x-1 for x in Atom2]
    Pos1 = average(Pos[Atom1], axis=0)
    Pos2 = average(Pos[Atom2], axis=0)
    Kind = 0
    if not Rest["ATOM3"] == -1:
      Kind = 1
      Pos3 = Pos[Rest["ATOM3"] - 1]
    if not Rest["ATOM4"] == -1:
      Kind = 2
      Pos4 = Pos[Rest["ATOM4"] - 1]
    #calculate
    if Kind == 0:
      return geometry.Length(Pos1 - Pos2)
    elif Kind == 1:
      return geometry.Angle(Pos1, Pos2, Pos3)
    else:
      return geometry.Dihedral(Pos1, Pos2, Pos3, Pos4)

def RestEnergyList(Pos, RestList):
  "Returns all the restraint energy for a restraint list."
  return [CalcRestEnergy(CalcRestDist(Pos, r), r) for r in RestList]
  
def RestEnergy(Pos, RestList):
  "Returns the total restraint energies for a restraint list."
  return sum(RestEnergyList(Pos, RestList))


#======== FUNCTIONS OPERATING ON PAIRS OF CLASSES ========

def SwapData(a, b):
  """Swaps class data in two sim classes.
* a, b: instances of SimClass"""
  for itm in ClassData:
    a.__dict__[itm], b.__dict__[itm] = b.__dict__[itm], a.__dict__[itm]

def SwapConfig(a, b, Mode = 0, RescaleVel = False):
  """Swaps the current configuration and related data of two sim classes
* a, b: instances of SimClass
* Mode: 0 will perform the swap on disk; 1 will perform in memory using
  the a.Pos and b.Pos variables."""
  if Mode == 0:
    #we need to make the changes on disk in this mode:
    #get the configurations
    Posa, Vela = a.GetPos(UseVel = True)
    Posb, Velb = b.GetPos(UseVel = True)
    #check if we need to rescale the velocities
    if RescaleVel:
      f = sqrt(a["TEMPSET"] / b["TEMPSET"])
      Vela = Vela / f
      Velb = Velb * f
    #set the configurations (swap a and b)
    a.SetPos(Posb, Velb)
    b.SetPos(Posa, Vela)
  #swap the related data; note that Pos and Vel are included in here as well
  if Mode == 1 and RescaleVel:
    f = sqrt(a["TEMPSET"] / b["TEMPSET"])
  for itm in ConfigData:
    a.__dict__[itm], b.__dict__[itm] = b.__dict__[itm], a.__dict__[itm]
  #if this was an in-memory swap, we have to correct the velocities
  if Mode == 1 and RescaleVel:
    a.Vel = a.Vel * f
    b.Vel = b.Vel / f

def SwapRest(a, b):
  """Swaps the restraints of two sim classes
* a, b: instances of SimClass"""
  a.RestList, b.RestList = b.RestList, a.RestList
  #positional restraints swap
  if a["POSRESTON"] or b["POSRESTON"]:
    #get the configurations
    cfga = a.GetRefPos()
    cfgb = b.GetRefPos()
    #set the configurations (swap a and b)
    a.SetRefPos(cfgb)
    b.SetRefPos(cfga)    

def SameRest(a, b):
  """Returns true if a and b have the same restraints."""
  Na, Nb = len(a.RestList), len(b.RestList)
  if not Na == Nb: return False
  for i in range(Na):
    ra, rb = a.RestList[i], b.RestList[i]
    if not IsSameRest(ra, rb): return False
  return True

    

#======== SAMPLE CODE ========
def SampleCode():
  sys.exit()

  #ALL OF THE CODE BELOW IS FOR DEMONSTRATION PURPOSES ONLY AND HAS NOT BEEN TESTED  

  #instantiate a new class; each class must have it's own path;
  #default is current directory
  test = SimClass()
  
  #tell the class what it will consist of; e.g.,
  #an extended sequence of specified composition:
  test.SysInitSeq("AAAA", Cap = True)
  #or
  test.SysInitPdb("mypdb.pdb")

  #build the system (compile using tleap)
  test.SysBuild()
  #optionally build using tleap commands from a file:
  test.SysBuild("tleapcommands.txt")

  #show all the variables associated with this class
  test.ShowAllData()
  #print the most recent simulation data (energies, etc)
  print test.Data
  #print individual variables
  print test["TEMPSET"], test["STEPSMD"], test["STEPSMIN1"], test["STEPSIZE"]
  #change individual variables (two ways to do this)
  test["STEPSIZE"] = 0.002
  test.SetStepSize(0.002)

  #add a user variable "DELTA_E" to the simclass:
  test["DELTA_E"] = test["EPOT2"] - test["EPOT1"]
  #list all user variables
  print test.UserData
  #delete the user variable
  del test["DELTA_E"]

  #clear all restraints    
  test.RestClear()
  #add a restraint between two atoms
  test.RestSetAtoms(2, 5)
  #add a restraint with 50% strength
  test.RestSetAtoms(4, 8, Strength = 0.5)
  #add a restraint between two residues' alpha carbon
  test.RestSetRes(2, 3, "CA", "CA")

  #run minimization for 50 steps steepest descent, 1000 CG    
  test.RunMin(50, 1000)

  #run molecular dynamics with default parameters and random seed    
  test.RunMD()
  print "Time for run was: %.1f sec" % test.ElapsedTime()
  print "Current storage requirement is: %.1f kb" % (float(test.StorageSize())/1024.,)
  #run MD with specific seed and parameters
  test.RunMD(Seed = 2342, NSteps = 2000, StepSize = 0.001)
  #reset parameters to other values
  test["NSTEPS"] = 500
  test["STEPSIZE"] = 0.002

  #print data from last run; suffixes '1' and '2' indicate start and stop of run
  print test.Data
  print "Initial and final potential energies: %.2f, %.2f" % (test["EPOT1"], test["EPOT2"])

  #get the history of all data at all time points from the last run
  d = s.GetHistory()
  avgepot = sum(d["EPOT"]) / float(len(d["EPOT"]))
  #get just the kinetic energy and temperature
  d = s.GetHistory(["EKIN","TEMP"])

  #set an undo point    
  test.UndoPrep()
  #run some md and check data
  test.RunMD()
  print test.Data
  #revert to last undo point
  test.UndoRun()
  print test.Data

  #save a restart file
  test.Save()
  #load the restart file
  test.Load()

  #get the current configuration and give it to a new sim class
  cfg = test.GetPos()
  test2 = SimClass("./newclass/")
  test2.SysInitSeq("AAAA", Cap = True)
  test2.SysBuild()
  test2.SetPos(cfg)

  #concatenate most recent trajectory data to master files,
  #i.e. to 0.mdtrj.crd.gz, 0.mdene.txt.gz, etc.
  test1.ConcatData("0", "./masterfiles/", Gzip = True)
  
  


#======== COMMAND LINE RUNNING ========

  
if __name__ == "__main__":

  def GetRestRes(s):
    l = s.split(",")
    l = [int(x) - 1 for x in l]
    return [(l[i], l[i+1]) for i in range(0, len(l), 2)]

  Args = scripttools.ParseArgs(sys.argv,
                               AcceptArg = ["min", "restrainres", "restraindist",
                                            "skipgzip", "temp", "langevin",
                                            "berendsen", "andersen", "removecom",
                                            "shakemode", "timestep"])
  RunPath = Args[1]
  TimeInPs = float(Args[2])
  Protein = " ".join(Args["ARGS"][3:])
  RunMin = "min" in Args["FLAGS"]
  if "restrainres" in Args:
    RestrainRes = GetRestRes(Args["restrainres"])
  else:
    RestrainRes = []
  if "restraindist" in Args:
    RestrainDist = float(Args["restraindist"])
  else:
    RestrainDist = RestDflts["DIST4"]
  SkipGzip = "skipgzip" in Args["FLAGS"]

  s = SimClass(RunPath)

  #check thermostat stuff
  if "temp" in Args:
    if "-" in Args["temp"]:
      Temp1, Temp2 = [float(x) for x in Args["temp"].split("-")[:2]]
      s["TEMPSET1"] = Temp1
      s["TEMPSET2"] = Temp2
    else:
      s["TEMPSET"] = float(Args["temp"])
  s["TEMPMODE"] = 0
  if "berendsen" in Args:
    s["TEMPMODE"] = 1
    s["BERENDSENTAU"] = float(Args["berendsen"])
  if "langevin" in Args:
    s["TEMPMODE"] = 3
    s["LANGEVINGAMMA"] = float(Args["langevin"])
  if "andersen" in Args:
    s["TEMPMODE"] = 2
    s["STEPSTEMP"] = float(Args["andersen"])

  if "removecom" in Args:
    s["STEPSREMOVECOM"] = int(Args["removecom"])
    print "Removing center of mass movement every %d steps" % s["STEPSREMOVECOM"]
  if "shakemode" in Args:
    s["SHAKEMODE"] = int(Args["shakemode"])
    print "Set SHAKE mode to %d" % s["SHAKEMODE"]    
  if "timestep" in Args:
    s["STEPSIZE"] = float(Args["timestep"])
    print "Set time step size to %.3f ps" % s["STEPSIZE"]
    
  if ".pdb" in Protein:
    print "Initializing from pdb file."
    s.SysInitPdb(Protein)
  else:
    print "Initializing from sequence."
    s.SysInitSeq(Protein)
  s.SysBuild()

  s["DIST4"] = RestrainDist
  s["DIST3"] = RestrainDist - 0.5
  for (a,b) in RestrainRes:
    print "Restraining residues %d and %d at %.1f A" % (a+1,b+1,RestrainDist)
    s.RestSetRes(a, b)
    
  if RunMin:
    print "Running minimization."
    s.RunMin()

  StepsMD = int(TimeInPs / s["STEPSIZE"] + 0.5)
  print "Running %d simulation steps (%.2f ps)" % (StepsMD, TimeInPs)
  s.RunMD(NSteps = StepsMD)

  if not SkipGzip:
    f = os.path.join(s.RunPath, "mdtrj.crd")
    if os.path.isfile(f):
      print "Gzipping trajectory"
      os.system("gzip -f -q %s" % f)
    
  
  
