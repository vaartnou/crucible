<?php
$token = trim(file_get_contents('/tmp/crucible-private-token'));
function api($method, $path, $body=null, $headers=[]) {
 global $token;
 $ch=curl_init('http://127.0.0.1:8000/v1'.$path);
 curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_CUSTOMREQUEST=>$method,CURLOPT_HTTPHEADER=>array_merge(['Authorization: Bearer '.$token,'Content-Type: application/json','Accept: application/json'],$headers),CURLOPT_TIMEOUT=>30]);
 if($body!==null) curl_setopt($ch,CURLOPT_POSTFIELDS,json_encode($body));
 $raw=curl_exec($ch); $status=curl_getinfo($ch,CURLINFO_HTTP_CODE);
 $data=json_decode($raw,true);
 if($status<200||$status>=300){fwrite(STDERR,json_encode(['method'=>$method,'path'=>$path,'status'=>$status,'error'=>is_array($data)?($data['message']??$data['error']??$data['errors']??$data):substr($raw,0,1000)])."\n");exit(1);}
 echo json_encode(['method'=>$method,'path'=>$path,'http'=>$status,'id'=>$data['id']??null,'status'=>$data['status']??null,'dispatched'=>$data['dispatched']??null])."\n";
 return $data;
}
$driver=api('POST','/drivers',['name'=>'Crucible Driver','status'=>'active','location'=>['type'=>'Point','coordinates'=>[24.94,60.17]]]);
$vehicle=api('POST','/vehicles',['name'=>'Crucible Van','plate_number'=>'CRUCIBLE-02','status'=>'available','location'=>['type'=>'Point','coordinates'=>[24.94,60.17]]]);
$body=['type'=>'transport','internal_id'=>'crucible-request-two','pickup'=>['street1'=>'Depot Road','city'=>'Helsinki','country'=>'FI','latitude'=>60.17,'longitude'=>24.94],'dropoff'=>['street1'=>'Customer Road','city'=>'Helsinki','country'=>'FI','latitude'=>60.18,'longitude'=>24.95],'meta'=>['crucible_request_id'=>'request-one']];
$order=api('POST','/orders',$body,['Idempotency-Key: crucible-request-two']);
$replay=api('POST','/orders',$body,['Idempotency-Key: crucible-request-two']);
if($order['id']!==$replay['id'])throw new Exception('Idempotency failed');
$id=$order['id'];
$order=api('PUT','/orders/'.$id,['driver'=>$driver['id'],'vehicle'=>$vehicle['id']]);
$order=api('PATCH','/orders/'.$id.'/dispatch');
$order=api('POST','/orders/'.$id.'/start');
for($n=0;$n<5&&$order['status']!=='completed';$n++){
 $next=api('GET','/orders/'.$id.'/next-activity');
 if(!isset($next[0])) throw new Exception('No next activity');
 $order=api('POST','/orders/'.$id.'/update-activity',['activity'=>$next[0]]);
}
$order=api('GET','/orders/'.$id);
if($order['status']!=='completed')throw new Exception('Not completed');
file_put_contents('/tmp/crucible-native-order-proof.json',json_encode($order,JSON_PRETTY_PRINT));
echo "native order workflow complete; physical verification remains simulator responsibility\n";
