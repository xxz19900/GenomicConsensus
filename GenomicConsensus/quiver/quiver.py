#################################################################################
# Copyright (c) 2011-2013, Pacific Biosciences of California, Inc.
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# * Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in the
#   documentation and/or other materials provided with the distribution.
# * Neither the name of Pacific Biosciences nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY
# THIS LICENSE.  THIS SOFTWARE IS PROVIDED BY PACIFIC BIOSCIENCES AND ITS
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL PACIFIC BIOSCIENCES OR
# ITS CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER
# IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#################################################################################

# Author: David Alexander

from __future__ import absolute_import


import collections, h5py, math, logging, numpy as np, os, pprint
import sys, time
from .. import reference
from ..options import options
from ..Worker import WorkerProcess, WorkerThread
from ..ResultCollector import ResultCollectorProcess, ResultCollectorThread
from ..io.VariantsGffWriter import VariantsGffWriter
from pbcore.io import (rangeQueries,
                       FastaWriter,
                       FastqWriter)

import ConsensusCore as cc
from GenomicConsensus.consensus import *
from GenomicConsensus.quiver.utils import *
from GenomicConsensus.quiver.model import *


class QuiverConfig(object):
    """
    Quiver configuration options
    """
    def __init__(self,
                 minMapQV=10,
                 minPoaCoverage=3,
                 maxPoaCoverage=11,
                 mutationSeparation=10,
                 mutationNeighborhood=20,
                 maxIterations=20,
                 refineDinucleotideRepeats=True,
                 noEvidenceConsensus="nocall",
                 computeConfidence=True,
                 readStumpinessThreshold=0.1,
                 parameters=None):

        self.minMapQV                   = minMapQV
        self.minPoaCoverage             = minPoaCoverage
        self.maxPoaCoverage             = maxPoaCoverage
        self.mutationSeparation         = mutationSeparation
        self.mutationNeighborhood       = mutationNeighborhood
        self.maxIterations              = maxIterations
        self.refineDinucleotideRepeats  = refineDinucleotideRepeats
        self.noEvidenceConsensus        = noEvidenceConsensus
        self.computeConfidence          = computeConfidence
        self.readStumpinessThreshold    = readStumpinessThreshold
        self.parameters                 = parameters or QuiverConfig._defaultQuiverParameters()

        # Convenience
        self.model                      = self.parameters.model
        self.ccQuiverConfig             = self.parameters.quiverConfig

    @staticmethod
    def _defaultQuiverParameters():
        return loadParameterSets(findParametersFile())["unknown.NoQVsModel"]


def quiverConsensusAndVariantsForWindow(cmpH5, refWindow, referenceContig,
                                        depthLimit, quiverConfig):
    """
    High-level routine for calling the consensus for a
    window of the genome given a cmp.h5.

    Identifies the coverage contours of the window in order to
    identify subintervals where a good consensus can be called.
    Creates the desired "no evidence consensus" where there is
    inadequate coverage.
    """
    winId, winStart, winEnd = refWindow
    refSequence = referenceContig[winStart:winEnd].tostring()
    logging.info("Quiver operating on %s" %
                 reference.windowToString(refWindow))

    noEvidenceConsensusFactory = \
        noEvidenceConsensusFactoryByName[quiverConfig.noEvidenceConsensus]

    if options.fancyChunking:
        # 1) identify the intervals with adequate coverage for quiver
        #    consensus; restrict to intervals of length > 10
        allRows = readsInWindow(cmpH5, refWindow, minMapQV=quiverConfig.minMapQV)
        starts = cmpH5.tStart[allRows]
        ends   = cmpH5.tEnd[allRows]
        intervals = [ (s, e)
                      for (s, e) in kSpannedIntervals(refWindow,
                                                      quiverConfig.minPoaCoverage,
                                                      starts,
                                                      ends)
                      if (e - s) > 10 ]

        coverageGaps = holes(refWindow, intervals)
        allIntervals = sorted(intervals + coverageGaps)
        if len(allIntervals) > 1:
            logging.info("Usable coverage in %s: %r" %
                         (reference.windowToString(refWindow), intervals))

    else:
        allIntervals = [ (winStart, winEnd) ]

    # 2) pull out the reads we will use for each interval
    # 3) call quiverConsensusForAlignments on the interval
    subConsensi = []
    variants = []

    for interval in allIntervals:
        intStart, intEnd = interval
        intRefSeq = referenceContig[intStart:intEnd].tostring()
        subWin = subWindow(refWindow, interval)

        windowRefSeq = referenceContig[intStart:intEnd].tostring()
        rows = readsInWindow(cmpH5, subWin,
                             depthLimit=depthLimit,
                             minMapQV=quiverConfig.minMapQV,
                             strategy="longest")
        alns = cmpH5[rows]
        clippedAlns_ = [ aln.clippedTo(*interval) for aln in alns ]
        clippedAlns = filterAlnsForQuiver(subWin, clippedAlns_, quiverConfig)

        if len([ a for a in clippedAlns
                 if a.spansReferenceRange(*interval) ]) >= quiverConfig.minPoaCoverage:

            css = quiverConsensusForAlignments(subWin,
                                               intRefSeq,
                                               clippedAlns,
                                               quiverConfig)

            siteCoverage = rangeQueries.getCoverageInRange(cmpH5, subWin, rows)
            variants_ = variantsFromConsensus(refWindow, refSequence,
                                              css.sequence, css.confidence, siteCoverage,
                                              options.aligner)
            variants += filterVariants(options.minCoverage,
                                       options.minConfidence,
                                       variants_)

            # Check for dump
            shouldDumpEvidence = \
                ((options.dumpEvidence == "all") or
                 (options.dumpEvidence == "variants") and (len(variants) > 0))
            if shouldDumpEvidence:
                dumpEvidence(options.evidenceDirectory,
                             subWin, windowRefSeq,
                             clippedAlns, css)
        else:
            css = noEvidenceConsensusFactory(subWin, intRefSeq)

        subConsensi.append(css)

    # 4) glue the subwindow consensus objects together to form the
    #    full window consensus
    css = join(subConsensi)

    # 5) Return
    return css, variants

def dumpEvidence(evidenceDumpBaseDirectory,
                 refWindow, refSequence, alns,
                 quiverConsensus):
    # Format of evidence dump:
    # evidence_dump/
    #   ref000001/
    #     0-1005/
    #       reference.fa
    #       reads.fa
    #       consensus.fa
    #       quiver-scores.h5
    #     995-2005/
    #       ...
    join = os.path.join
    refId, refStart, refEnd = refWindow
    refName = reference.idToName(refId)
    windowDirectory = join(evidenceDumpBaseDirectory,
                           refName,
                           "%d-%d" % (refStart, refEnd))
    logging.info("Dumping evidence to %s" % (windowDirectory,))

    if os.path.exists(windowDirectory):
        raise Exception, "Evidence dump does not expect directory %s to exist." % windowDirectory
    os.makedirs(windowDirectory)
    refFasta       = FastaWriter(join(windowDirectory, "reference.fa"))
    readsFasta     = FastaWriter(join(windowDirectory, "reads.fa"))
    consensusFasta = FastaWriter(join(windowDirectory, "consensus.fa"))

    windowName = refName + (":%d-%d" % (refStart, refEnd))
    refFasta.writeRecord(windowName, refSequence)
    refFasta.close()

    consensusFasta.writeRecord(windowName + "|quiver", quiverConsensus.sequence)
    consensusFasta.close()

    rowNames, columnNames, scores = scoreMatrix(quiverConsensus.mms)
    quiverScoreFile = h5py.File(join(windowDirectory, "quiver-scores.h5"))
    quiverScoreFile.create_dataset("Scores", data=scores)
    quiverScoreFile.create_dataset("RowNames", data=rowNames)
    quiverScoreFile.create_dataset("ColumnNames", data=columnNames)
    quiverScoreFile.close()
    for aln in alns:
        readsFasta.writeRecord(aln.readName, aln.read(orientation="genomic", aligned=False))
    readsFasta.close()


class QuiverWorker(object):

    @property
    def quiverConfig(self):
        return self._algorithmConfig

    def onChunk(self, workChunk):
        referenceWindow  = workChunk.window
        refId, refStart, refEnd = referenceWindow

        refSeqInWindow = reference.sequenceInWindow(referenceWindow)
        noCallFn = noEvidenceConsensusFactoryByName[self.quiverConfig.noEvidenceConsensus]

        # Quick cutout for no-coverage case
        if not workChunk.hasCoverage:
            noCallCss = noCallFn(referenceWindow, refSeqInWindow)
            return (referenceWindow, (noCallCss, []))

        # General case
        eWindow = reference.enlargedReferenceWindow(referenceWindow,
                                                    options.referenceChunkOverlap)
        _, eStart, eEnd = eWindow

        # We call consensus on the enlarged window and then map back
        # to the reference and clip the consensus at the implied
        # bounds.  This seems to be more reliable thank cutting the
        # consensus bluntly
        refContig = reference.byId[refId].sequence
        refSequenceInEnlargedWindow = refContig[eStart:eEnd].tostring()

        #
        # Get the consensus for the enlarged window.
        #
        css_, variants_ = \
            quiverConsensusAndVariantsForWindow(self._inCmpH5, eWindow,
                                                refContig, options.coverage, self.quiverConfig)

        #
        # Restrict the consensus and variants to the reference window.
        #
        ga = cc.Align(refSequenceInEnlargedWindow, css_.sequence)
        targetPositions = cc.TargetToQueryPositions(ga)
        cssStart = targetPositions[refStart-eStart]
        cssEnd   = targetPositions[refEnd-eStart]

        cssSequence    = css_.sequence[cssStart:cssEnd]
        cssQv          = css_.confidence[cssStart:cssEnd]
        variants       = [ v for v in variants_
                           if refStart <= v.refStart < refEnd ]

        consensusObj = Consensus(referenceWindow,
                                 cssSequence,
                                 cssQv)

        return (referenceWindow, (consensusObj, variants))



#
# Slave process/thread classes
#
class QuiverWorkerProcess(QuiverWorker, WorkerProcess): pass
class QuiverWorkerThread(QuiverWorker, WorkerThread): pass


#
# Plugin API
#
__all__ = [ "name",
            "availability",
            "additionalDefaultOptions",
            "configure",
            "slaveFactories" ]

name = "Quiver"
availability = (True, "OK")
additionalDefaultOptions = { "referenceChunkOverlap"      : 5,
                             "parameterSet"               : "best" }


def fetchParameterSet(cmpH5, parametersFileOrDirectory, parameterSetName):
    parametersFile = findParametersFile(parametersFileOrDirectory)
    logging.info("Using Quiver parameter sets from %s" % parametersFile)
    parameterSets = loadParameterSets(parametersFile)
    if parameterSetName == "best":
        chemistry = majorityChemistry(cmpH5)
        params = bestParameterSet(parameterSets.values(),
                                  chemistry,
                                  cmpH5.pulseFeaturesAvailable())
    else:
        try:
            params = parameterSets[parameterSetName]
        except:
            die("Quiver: no available parameter set named %s" % parameterSetName)
    return params


def configure(options, cmpH5):
    if cmpH5.readType != "standard":
        raise IncompatibleDataException(
            "The Quiver algorithm requires a cmp.h5 file containing standard (non-CCS) reads." )

    params = fetchParameterSet(cmpH5,
                               options.parametersFile,
                               options.parameterSet)
    logging.info("Using Quiver parameter set %s" % params.name)

    if not params.model.isCompatibleWithCmpH5(cmpH5):
        raise IncompatibleDataException(
            "Selected Quiver parameter set is incompatible with this cmp.h5 file " +
            "due to missing data tracks.")

    if options.parameterSet == "best" and not allQVsLoaded(cmpH5):
        logging.warn(
            "This .cmp.h5 file lacks some of the QV data tracks that are required " +
            "for optimal performance of the Quiver algorithm.  For optimal results" +
            " use the ResequencingQVs workflow in SMRTPortal with bas.h5 files "    +
            "from an instrument using software version 1.3.1 or later.")

    quiverConfig = QuiverConfig(minMapQV=options.minMapQV,
                                noEvidenceConsensus=options.noEvidenceConsensusCall,
                                parameters=params)
    return quiverConfig


def slaveFactories(threaded):
    # By default we use slave processes. The tuple ordering is important.
    if threaded:
        return (QuiverWorkerThread,  ResultCollectorThread)
    else:
        return (QuiverWorkerProcess, ResultCollectorProcess)
