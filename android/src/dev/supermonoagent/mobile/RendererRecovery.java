package dev.supermonoagent.mobile;

/** Native lifecycle policy. Renderer identity is compared by reference, never by page URL. */
public final class RendererRecovery {
    public enum Action { IGNORE, RELOAD_HEALTHY, WAIT_FOR_RENDERER, FAIL_PENDING,
        RECREATE_GOOD, RETIRED_RECOVER, RETIRED_CANCELLED }
    private Object current,retiring;
    private long currentGeneration,retiringGeneration;
    private boolean pending,claimed,resumeAfterRetirement;

    public void attach(Object view,long generation,boolean startupPending) {
        if(retiring!=null)throw new IllegalStateException("Renderer retirement is not confirmed");
        current=view;currentGeneration=generation;pending=startupPending;claimed=false;
    }
    public boolean matches(Object view,long generation){return view!=null&&current==view&&currentGeneration==generation;}
    public boolean waitingForRenderer(){return retiring!=null;}
    public boolean isRetiring(Object view){return view!=null&&retiring==view;}
    public boolean ready(Object view,long generation){
        if(!matches(view,generation)||claimed)return false;pending=false;return true;
    }
    public Action failure(Object view,long generation,boolean hung) {
        if(!matches(view,generation)||!pending||claimed)return Action.IGNORE;
        claimed=true;pending=false;
        if(!hung)return Action.RELOAD_HEALTHY;
        retiring=view;retiringGeneration=generation;resumeAfterRetirement=true;
        return Action.WAIT_FOR_RENDERER;
    }
    public Action rendererGone(Object view,long generation) {
        if(retiring==view&&view!=null&&retiringGeneration==generation)return finishRetirement();
        if(!matches(view,generation))return Action.IGNORE;
        current=null;
        if(pending&&!claimed){pending=false;claimed=true;return Action.FAIL_PENDING;}
        return Action.RECREATE_GOOD;
    }
    /** Only used for API26-28 best effort, or to abandon an unavailable terminate API. */
    public Action finishWithoutTermination(Object view,long generation) {
        return retiring==view&&view!=null&&retiringGeneration==generation?finishRetirement():Action.IGNORE;
    }
    private Action finishRetirement(){
        boolean resume=resumeAfterRetirement;retiring=null;current=null;pending=false;claimed=true;resumeAfterRetirement=false;
        return resume?Action.RETIRED_RECOVER:Action.RETIRED_CANCELLED;
    }
    /** Cancel queued recovery, but keep the renderer latch until its death is confirmed. */
    public void cancel(){current=null;pending=false;claimed=true;resumeAfterRetirement=false;}
}
